package com.bookmap.orderflow.lst;

import java.awt.Color;
import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.SliceEventTracker;
import com.bookmap.orderflow.core.SliceEventTracker.Config;
import com.bookmap.orderflow.core.SliceEventTracker.EpisodeSnapshot;
import com.bookmap.orderflow.core.SliceEventTracker.Signal;
import com.bookmap.orderflow.core.SliceEventTracker.SignalKind;
import com.bookmap.orderflow.core.SliceEventTracker.State;

import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.common.Log;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyIndicator.GraphType;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.AxisGroup;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.MultiInstrumentListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * LST Momentum indicator (RULE_SPEC_V1): markers + alerts + quality readouts.
 * No order placement. Attach on NQ with ES also open.
 *
 * <p>Prefix {@code LST …}. Structured CSV signal log for parity / future autotrade.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("LST Momentum")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class LstMomentumModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        HistoricalDataListener,
        SliceEventTracker.Listener {

    @Parameter(name = "Use frozen V1 preset", step = 1.0)
    Boolean frozenPreset = true;

    @Parameter(name = "Trend min pts", step = 0.25)
    Double trendMinPts = 0.0;

    @Parameter(name = "Dom vs med min", step = 0.1)
    Double domVsMedMin = 4.0;

    @Parameter(name = "Stop buffer pts", step = 0.25)
    Double stopBufferPts = 2.0;

    @Parameter(name = "Cooldown sec", step = 1.0)
    Double cooldownSec = 30.0;

    @Parameter(name = "Show longs", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts", step = 1.0)
    Boolean showShorts = true;

    @Parameter(name = "Enable NQ", step = 1.0)
    Boolean enableNq = true;

    @Parameter(name = "Enable ES confirm", step = 1.0)
    Boolean enableEs = true;

    @Parameter(name = "Alert on signal", step = 1.0)
    Boolean alertOnSignal = true;

    @Parameter(name = "Signal log path (blank=off)")
    String signalLogPath = "lst_momentum_signals.csv";

    private String primaryAlias;
    private String currentAlias;
    private final Map<String, String> roleByAlias = new HashMap<>();
    private double pips = 0.25;
    private long nowNs;
    private SliceEventTracker tracker;
    private BufferedWriter logWriter;
    private boolean logHeaderWritten;

    private Indicator stateIndicator;
    private Indicator burstTpsIndicator;
    private Indicator domVsMedIndicator;
    private Indicator distToLevelIndicator;
    private Indicator pullbackRatioIndicator;
    private Indicator trendIndicator;
    private Indicator levelLongMarker;
    private Indicator levelShortMarker;
    private Indicator stopLongMarker;
    private Indicator stopShortMarker;
    private Indicator fireLongMarker;
    private Indicator fireShortMarker;

    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private double armedLevel = Double.NaN;
    private double armedStop = Double.NaN;
    private int armedSide;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.primaryAlias = alias;
        this.currentAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        roleByAlias.put(alias, InstrumentClassifier.classify(info));

        Config cfg = buildConfig();
        tracker = new SliceEventTracker(cfg, this);
        tracker.setTickSize(pips);

        stateIndicator = api.registerIndicator("LST State", GraphType.BOTTOM);
        burstTpsIndicator = api.registerIndicator("LST Burst tps", GraphType.BOTTOM);
        domVsMedIndicator = api.registerIndicator("LST Dom vs med", GraphType.BOTTOM);
        distToLevelIndicator = api.registerIndicator("LST Dist level", GraphType.BOTTOM);
        pullbackRatioIndicator = api.registerIndicator("LST PB ratio", GraphType.BOTTOM);
        trendIndicator = api.registerIndicator("LST Trend 5m", GraphType.BOTTOM);

        levelLongMarker = api.registerIndicator("LST Level Long", GraphType.PRIMARY);
        levelShortMarker = api.registerIndicator("LST Level Short", GraphType.PRIMARY);
        stopLongMarker = api.registerIndicator("LST Stop Long", GraphType.PRIMARY);
        stopShortMarker = api.registerIndicator("LST Stop Short", GraphType.PRIMARY);
        fireLongMarker = api.registerIndicator("LST Fire Long", GraphType.PRIMARY);
        fireShortMarker = api.registerIndicator("LST Fire Short", GraphType.PRIMARY);

        stateIndicator.setColor(new Color(200, 200, 200));
        burstTpsIndicator.setColor(new Color(0, 200, 255));
        domVsMedIndicator.setColor(new Color(255, 180, 40));
        distToLevelIndicator.setColor(new Color(180, 120, 255));
        pullbackRatioIndicator.setColor(new Color(255, 100, 160));
        trendIndicator.setColor(new Color(120, 220, 120));
        levelLongMarker.setColor(new Color(0, 180, 80));
        levelShortMarker.setColor(new Color(220, 60, 60));
        stopLongMarker.setColor(new Color(0, 120, 60));
        stopShortMarker.setColor(new Color(160, 40, 40));
        fireLongMarker.setColor(new Color(0, 255, 120));
        fireShortMarker.setColor(new Color(255, 80, 80));

        AxisGroup bottom = new AxisGroup();
        bottom.add(stateIndicator);
        bottom.add(burstTpsIndicator);
        bottom.add(domVsMedIndicator);
        bottom.add(distToLevelIndicator);
        bottom.add(pullbackRatioIndicator);
        bottom.add(trendIndicator);

        openLog();
        Log.info("LST Momentum init alias=" + alias + " role=" + roleByAlias.get(alias));
    }

    private Config buildConfig() {
        Config cfg = new Config();
        cfg.enableMomentum = true;
        cfg.enablePitchCatch = false; // separate module
        if (Boolean.TRUE.equals(frozenPreset)) {
            cfg.trendMinPts = 0.0;
            cfg.domVsMedMin = 4.0;
            cfg.stopBufferPts = 2.0;
            cfg.cooldownNs = 30_000_000_000L;
        } else {
            cfg.trendMinPts = trendMinPts == null ? 0.0 : trendMinPts;
            cfg.domVsMedMin = domVsMedMin == null ? 4.0 : domVsMedMin;
            cfg.stopBufferPts = stopBufferPts == null ? 2.0 : stopBufferPts;
            cfg.cooldownNs = (long) ((cooldownSec == null ? 30.0 : cooldownSec) * 1e9);
        }
        cfg.enableLongs = !Boolean.FALSE.equals(showLongs);
        cfg.enableShorts = !Boolean.FALSE.equals(showShorts);
        return cfg;
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : info.symbol;
        roleByAlias.put(alias, InstrumentClassifier.classify(info));
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        roleByAlias.putIfAbsent(alias, "OTHER");
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        nowNs = nanoseconds;
        if (tracker != null) {
            tracker.onTime(nanoseconds);
        }
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        EpisodeSnapshot snap = tracker == null ? EpisodeSnapshot.idle() : tracker.snapshot();
        stateIndicator.addPoint(stateCode(snap.state));
        burstTpsIndicator.addPoint(nan(snap.burstTps));
        domVsMedIndicator.addPoint(nan(snap.domVsMed));
        trendIndicator.addPoint(nan(snap.trend5mPts));
        pullbackRatioIndicator.addPoint(nan(snap.pullbackRatio));
        double dist = Double.NaN;
        if (!Double.isNaN(snap.dominantLevelPx) && !Double.isNaN(nqLastForDist)) {
            dist = Math.abs(nqLastForDist - snap.dominantLevelPx);
        }
        distToLevelIndicator.addPoint(nan(dist));

        if (snap.state == State.STACK_ARMED || snap.state == State.SLICING) {
            armedLevel = snap.dominantLevelPx;
            armedSide = snap.side;
            if (!Double.isNaN(armedLevel)) {
                armedStop = armedLevel - armedSide * (stopBufferPts == null ? 2.0 : stopBufferPts);
            }
        }
        paintLines(snap);
    }

    private double nqLastForDist = Double.NaN;

    private void paintLines(EpisodeSnapshot snap) {
        boolean showL = !Boolean.FALSE.equals(showLongs);
        boolean showS = !Boolean.FALSE.equals(showShorts);
        boolean armed = snap.state == State.STACK_ARMED || snap.state == State.SLICING;
        if (armed && armedSide > 0 && showL && !Double.isNaN(armedLevel)) {
            levelLongMarker.addPoint(armedLevel);
            stopLongMarker.addPoint(armedStop);
        } else {
            levelLongMarker.addPoint(Double.NaN);
            stopLongMarker.addPoint(Double.NaN);
        }
        if (armed && armedSide < 0 && showS && !Double.isNaN(armedLevel)) {
            levelShortMarker.addPoint(armedLevel);
            stopShortMarker.addPoint(armedStop);
        } else {
            levelShortMarker.addPoint(Double.NaN);
            stopShortMarker.addPoint(Double.NaN);
        }
        fireLongMarker.addPoint(lastFireLong);
        fireShortMarker.addPoint(lastFireShort);
        // Clear fire markers after one interval paint
        lastFireLong = Double.NaN;
        lastFireShort = Double.NaN;
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        if (tracker == null) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        boolean buy = tradeInfo.isBidAggressor;
        double px = PriceTicks.toChartPoints(price, pips);
        if ("NQ".equals(role)) {
            nqLastForDist = px;
            if (!Boolean.FALSE.equals(enableNq)) {
                tracker.onNqTrade(nowNs, px, size, buy);
            }
        } else if ("ES".equals(role)) {
            if (!Boolean.FALSE.equals(enableEs)) {
                tracker.onEsTrade(nowNs, px, size, buy);
            }
        }
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        if (tracker == null) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        if ("NQ".equals(role) || "ES".equals(role)) {
            tracker.onDepth(role, isBid, price, size);
        }
    }

    @Override
    public void onSignal(Signal signal) {
        if (signal.kind != SignalKind.MOMENTUM) {
            return;
        }
        if (signal.side > 0) {
            lastFireLong = signal.entryRefPx;
        } else {
            lastFireShort = signal.entryRefPx;
        }
        if (Boolean.TRUE.equals(alertOnSignal)) {
            Log.info(String.format(Locale.US,
                    "LST Momentum FIRE side=%d px=%.2f level=%.2f stop=%.2f trend=%.1f dom=%.2f tps=%.0f pb=%.2f",
                    signal.side, signal.entryRefPx, signal.dominantLevelPx, signal.stopPx,
                    signal.trend5mPts, signal.domVsMed, signal.burstTps, signal.pullbackRatio));
        }
        appendLog(signal);
    }

    @Override
    public void onState(State state, long tsNs) {
        // painted on interval
    }

    private static int stateCode(State s) {
        switch (s) {
            case STACK_ARMED: return 1;
            case SLICING: return 2;
            case CLEARED_LEFT_BEHIND: return 3;
            case ABSORBED: return -1;
            default: return 0;
        }
    }

    private static double nan(double v) {
        return Double.isFinite(v) ? v : Double.NaN;
    }

    private void openLog() {
        if (signalLogPath == null || signalLogPath.trim().isEmpty()) {
            return;
        }
        try {
            Path p = Paths.get(signalLogPath.trim());
            boolean exists = Files.exists(p);
            logWriter = Files.newBufferedWriter(p, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
            if (!exists) {
                logWriter.write(Signal.csvHeader());
                logWriter.newLine();
                logHeaderWritten = true;
            } else {
                logHeaderWritten = true;
            }
            logWriter.flush();
        } catch (IOException e) {
            Log.warn("LST Momentum: cannot open signal log: " + e.getMessage());
            logWriter = null;
        }
    }

    private void appendLog(Signal signal) {
        if (logWriter == null) {
            return;
        }
        try {
            if (!logHeaderWritten) {
                logWriter.write(Signal.csvHeader());
                logWriter.newLine();
                logHeaderWritten = true;
            }
            logWriter.write(signal.toCsvRow());
            logWriter.newLine();
            logWriter.flush();
        } catch (IOException e) {
            Log.warn("LST Momentum: log write failed: " + e.getMessage());
        }
    }

    @Override
    public void stop() {
        if (logWriter != null) {
            try {
                logWriter.close();
            } catch (IOException ignored) {
            }
            logWriter = null;
        }
    }
}
