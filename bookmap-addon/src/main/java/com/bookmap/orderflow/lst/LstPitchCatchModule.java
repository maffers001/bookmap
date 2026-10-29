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
 * LST Pitch-and-Catch absorption fade (RULE_SPEC_V1 / PC_V1).
 *
 * <p>Distinct from the existing {@code Pitch and Catch} (HV pitch / LV catch)
 * module — all indicators use the {@code LST-PC …} prefix. No order placement.
 * Attach on NQ with ES also open.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("LST Pitch-and-Catch")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class LstPitchCatchModule implements
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

    @Parameter(name = "PC pullback pts min", step = 0.25)
    Double pcPullbackPtsMin = 4.0;

    @Parameter(name = "PC pullback ratio min", step = 0.05)
    Double pcPullbackRatioMin = 0.70;

    @Parameter(name = "PC slow ratio max", step = 0.05)
    Double pcSlowRatioMax = 0.85;

    @Parameter(name = "PC net pts max", step = 0.25)
    Double pcNetPtsMax = 1.0;

    @Parameter(name = "Stop buffer pts", step = 0.25)
    Double stopBufferPts = 2.0;

    @Parameter(name = "Cooldown sec", step = 1.0)
    Double cooldownSec = 30.0;

    @Parameter(name = "Show longs (fade)", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts (fade)", step = 1.0)
    Boolean showShorts = true;

    @Parameter(name = "Alert on signal", step = 1.0)
    Boolean alertOnSignal = true;

    @Parameter(name = "Signal log path (blank=off)")
    String signalLogPath = "lst_pc_signals.csv";

    private String currentAlias;
    private final Map<String, String> roleByAlias = new HashMap<>();
    private double pips = 0.25;
    private long nowNs;
    private SliceEventTracker tracker;
    private BufferedWriter logWriter;
    private boolean logHeaderWritten;

    private Indicator stateIndicator;
    private Indicator pullbackRatioIndicator;
    private Indicator slowRatioIndicator;
    private Indicator stallDurIndicator;
    private Indicator pitchVolIndicator;
    private Indicator pitchLongMarker;
    private Indicator pitchShortMarker;
    private Indicator stopLongMarker;
    private Indicator stopShortMarker;
    private Indicator fireLongMarker;
    private Indicator fireShortMarker;
    private Indicator levelMarker;

    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private double lastSlowRatio = Double.NaN;
    private double lastStallDur = Double.NaN;
    private double armedPitch = Double.NaN;
    private double armedStop = Double.NaN;
    private double armedLevel = Double.NaN;
    private int armedBurstSide;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.currentAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        roleByAlias.put(alias, InstrumentClassifier.classify(info));

        tracker = new SliceEventTracker(buildConfig(), this);
        tracker.setTickSize(pips);

        stateIndicator = api.registerIndicator("LST-PC State", GraphType.BOTTOM);
        pullbackRatioIndicator = api.registerIndicator("LST-PC PB ratio", GraphType.BOTTOM);
        slowRatioIndicator = api.registerIndicator("LST-PC Tape slow", GraphType.BOTTOM);
        stallDurIndicator = api.registerIndicator("LST-PC Stall s", GraphType.BOTTOM);
        pitchVolIndicator = api.registerIndicator("LST-PC Burst tps", GraphType.BOTTOM);

        pitchLongMarker = api.registerIndicator("LST-PC Pitch Long", GraphType.PRIMARY);
        pitchShortMarker = api.registerIndicator("LST-PC Pitch Short", GraphType.PRIMARY);
        stopLongMarker = api.registerIndicator("LST-PC Stop Long", GraphType.PRIMARY);
        stopShortMarker = api.registerIndicator("LST-PC Stop Short", GraphType.PRIMARY);
        fireLongMarker = api.registerIndicator("LST-PC Fire Long", GraphType.PRIMARY);
        fireShortMarker = api.registerIndicator("LST-PC Fire Short", GraphType.PRIMARY);
        levelMarker = api.registerIndicator("LST-PC Dom Level", GraphType.PRIMARY);

        stateIndicator.setColor(new Color(200, 200, 200));
        pullbackRatioIndicator.setColor(new Color(255, 100, 160));
        slowRatioIndicator.setColor(new Color(100, 180, 255));
        stallDurIndicator.setColor(new Color(255, 200, 60));
        pitchVolIndicator.setColor(new Color(180, 120, 255));
        pitchLongMarker.setColor(new Color(0, 160, 220));
        pitchShortMarker.setColor(new Color(220, 120, 0));
        stopLongMarker.setColor(new Color(0, 100, 160));
        stopShortMarker.setColor(new Color(160, 80, 0));
        fireLongMarker.setColor(new Color(0, 255, 200));
        fireShortMarker.setColor(new Color(255, 140, 0));
        levelMarker.setColor(new Color(160, 160, 160));

        AxisGroup bottom = new AxisGroup();
        bottom.add(stateIndicator);
        bottom.add(pullbackRatioIndicator);
        bottom.add(slowRatioIndicator);
        bottom.add(stallDurIndicator);
        bottom.add(pitchVolIndicator);

        openLog();
        Log.info("LST-PC init alias=" + alias + " role=" + roleByAlias.get(alias));
    }

    private Config buildConfig() {
        Config cfg = new Config();
        cfg.enableMomentum = false;
        cfg.enablePitchCatch = true;
        if (Boolean.TRUE.equals(frozenPreset)) {
            cfg.pcPullbackPtsMin = 4.0;
            cfg.pcPullbackRatioMin = 0.70;
            cfg.pcSlowRatioMax = 0.85;
            cfg.pcNetPtsMax = 1.0;
            cfg.stopBufferPts = 2.0;
            cfg.cooldownNs = 30_000_000_000L;
        } else {
            cfg.pcPullbackPtsMin = pcPullbackPtsMin == null ? 4.0 : pcPullbackPtsMin;
            cfg.pcPullbackRatioMin = pcPullbackRatioMin == null ? 0.70 : pcPullbackRatioMin;
            cfg.pcSlowRatioMax = pcSlowRatioMax == null ? 0.85 : pcSlowRatioMax;
            cfg.pcNetPtsMax = pcNetPtsMax == null ? 1.0 : pcNetPtsMax;
            cfg.stopBufferPts = stopBufferPts == null ? 2.0 : stopBufferPts;
            cfg.cooldownNs = (long) ((cooldownSec == null ? 30.0 : cooldownSec) * 1e9);
        }
        // Fade long = short burst absorbed; allow both burst sides
        cfg.enableLongs = true;
        cfg.enableShorts = true;
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
        pullbackRatioIndicator.addPoint(finite(snap.pullbackRatio));
        slowRatioIndicator.addPoint(finite(lastSlowRatio));
        stallDurIndicator.addPoint(finite(lastStallDur));
        pitchVolIndicator.addPoint(finite(snap.burstTps));

        boolean armed = snap.state == State.STACK_ARMED || snap.state == State.SLICING;
        if (armed) {
            armedPitch = snap.pitchExtremePx;
            armedLevel = snap.dominantLevelPx;
            armedBurstSide = snap.side;
            if (!Double.isNaN(armedPitch)) {
                int fadeSide = -armedBurstSide;
                double buf = stopBufferPts == null ? 2.0 : stopBufferPts;
                armedStop = armedPitch - fadeSide * buf;
            }
        }

        boolean showL = !Boolean.FALSE.equals(showLongs);
        boolean showS = !Boolean.FALSE.equals(showShorts);
        // Pitch of a long burst → potential short fade; markers follow burst side for pitch
        if (armed && armedBurstSide > 0 && showS && !Double.isNaN(armedPitch)) {
            pitchShortMarker.addPoint(armedPitch);
            stopShortMarker.addPoint(armedStop);
        } else {
            pitchShortMarker.addPoint(Double.NaN);
            stopShortMarker.addPoint(Double.NaN);
        }
        if (armed && armedBurstSide < 0 && showL && !Double.isNaN(armedPitch)) {
            pitchLongMarker.addPoint(armedPitch);
            stopLongMarker.addPoint(armedStop);
        } else {
            pitchLongMarker.addPoint(Double.NaN);
            stopLongMarker.addPoint(Double.NaN);
        }
        levelMarker.addPoint(armed && !Double.isNaN(armedLevel) ? armedLevel : Double.NaN);
        fireLongMarker.addPoint(lastFireLong);
        fireShortMarker.addPoint(lastFireShort);
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
            tracker.onNqTrade(nowNs, px, size, buy);
        } else if ("ES".equals(role)) {
            tracker.onEsTrade(nowNs, px, size, buy);
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
        if (signal.kind != SignalKind.PITCH_CATCH) {
            return;
        }
        lastSlowRatio = signal.postSlowRatio;
        lastStallDur = signal.stallDurS;
        if (signal.side > 0) {
            lastFireLong = signal.entryRefPx;
        } else {
            lastFireShort = signal.entryRefPx;
        }
        if (Boolean.TRUE.equals(alertOnSignal)) {
            Log.info(String.format(Locale.US,
                    "LST-PC FIRE fadeSide=%d px=%.2f pitch=%.2f stop=%.2f level=%.2f pb=%.2f slow=%.2f stall=%.1fs",
                    signal.side, signal.entryRefPx, signal.pitchExtremePx, signal.stopPx,
                    signal.dominantLevelPx, signal.pullbackRatio, signal.postSlowRatio, signal.stallDurS));
        }
        appendLog(signal);
    }

    @Override
    public void onState(State state, long tsNs) {
        // interval paint
    }

    private static int stateCode(State s) {
        switch (s) {
            case STACK_ARMED: return 1;
            case SLICING: return 2;
            case ABSORBED: return 3;
            case CLEARED_LEFT_BEHIND: return -1;
            default: return 0;
        }
    }

    private static double finite(double v) {
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
            }
            logHeaderWritten = true;
            logWriter.flush();
        } catch (IOException e) {
            Log.warn("LST-PC: cannot open signal log: " + e.getMessage());
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
            Log.warn("LST-PC: log write failed: " + e.getMessage());
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
