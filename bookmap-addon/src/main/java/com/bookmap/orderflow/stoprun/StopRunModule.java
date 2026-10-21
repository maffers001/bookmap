package com.bookmap.orderflow.stoprun;

import java.awt.Color;
import java.time.LocalTime;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.StopRunTracker;
import com.bookmap.orderflow.core.StopRunTracker.Resolve;
import com.bookmap.orderflow.core.StopRunTracker.Side;
import com.bookmap.orderflow.core.StopRunTracker.Signal;

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
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.MarketByOrderDepthDataListener;
import velox.api.layer1.simplified.MultiInstrumentListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Stop-run fingerprint marker (EXP-0041 / H32). Visual only — no auto-trade.
 *
 * <p>Attach on NQ with ES also open. Frozen research defaults:
 * pierce=2 ticks, window=2000ms, min_vol=25, min_imb=0.60, min_sweeps=1,
 * min_aggr=3, min_score=2.
 *
 * <ul>
 *   <li>{@code SR Level High/Low} — nearest live unpierced reference level</li>
 *   <li>{@code SR Run Long/Short} — marker at fire (continuation side)</li>
 *   <li>{@code SR Fade Long/Short} — marker if reclaim (stall) within reclaim sec</li>
 *   <li>{@code SR Score} — last fire score 0–4</li>
 *   <li>{@code SR Signal} — brief +/-1 on fire</li>
 * </ul>
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Stop Run")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class StopRunModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        MarketByOrderDepthDataListener,
        HistoricalDataListener {

    @Parameter(name = "Pierce ticks", step = 1.0)
    Double pierceTicks = 2.0;

    @Parameter(name = "Window ms", step = 100.0)
    Double windowMs = 2000.0;

    @Parameter(name = "Min vol", step = 1.0)
    Double minVol = 25.0;

    @Parameter(name = "Min prints", step = 1.0)
    Double minPrints = 8.0;

    @Parameter(name = "Min imbalance", step = 0.05)
    Double minImbalance = 0.60;

    @Parameter(name = "Min sweeps", step = 1.0)
    Double minSweeps = 1.0;

    @Parameter(name = "Min aggressors", step = 1.0)
    Double minAggressors = 3.0;

    @Parameter(name = "Min score", step = 1.0)
    Double minScore = 2.0;

    @Parameter(name = "Pullaway pts", step = 1.0)
    Double pullawayPts = 20.0;

    @Parameter(name = "Lookback sec", step = 30.0)
    Double lookbackSec = 600.0;

    @Parameter(name = "Reclaim sec", step = 1.0)
    Double reclaimSec = 5.0;

    @Parameter(name = "Cooldown sec", step = 1.0)
    Double cooldownSec = 5.0;

    @Parameter(name = "Sync ms", step = 100.0)
    Double syncMs = 1500.0;

    @Parameter(name = "Use swings", step = 1.0)
    Boolean useSwings = true;

    @Parameter(name = "Use session H/L", step = 1.0)
    Boolean useSession = true;

    @Parameter(name = "Use overnight H/L", step = 1.0)
    Boolean useOvernight = true;

    @Parameter(name = "Show longs", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts", step = 1.0)
    Boolean showShorts = true;

    private double pips = 0.25;
    private long nowNs;
    private String primaryAlias;
    private String currentAlias;
    private String nqAlias;
    private final Map<String, String> roleByAlias = new HashMap<>();

    private StopRunTracker tracker;

    private Indicator signalIndicator;
    private Indicator scoreIndicator;
    private Indicator levelHigh;
    private Indicator levelLow;
    private Indicator runLong;
    private Indicator runShort;
    private Indicator fadeLong;
    private Indicator fadeShort;

    private int lastSignalSide;
    private int signalHold;
    private double lastRunLong = Double.NaN;
    private double lastRunShort = Double.NaN;
    private double lastFadeLong = Double.NaN;
    private double lastFadeShort = Double.NaN;
    private int lastScore;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.primaryAlias = alias;
        this.currentAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        String role = InstrumentClassifier.classify(info);
        roleByAlias.put(alias, role);
        if ("NQ".equals(role)) {
            nqAlias = alias;
        }

        tracker = buildTracker();

        signalIndicator = api.registerIndicator("SR Signal", GraphType.BOTTOM);
        scoreIndicator = api.registerIndicator("SR Score", GraphType.BOTTOM);
        levelHigh = api.registerIndicator("SR Level High", GraphType.PRIMARY);
        levelLow = api.registerIndicator("SR Level Low", GraphType.PRIMARY);
        runLong = api.registerIndicator("SR Run Long", GraphType.PRIMARY);
        runShort = api.registerIndicator("SR Run Short", GraphType.PRIMARY);
        fadeLong = api.registerIndicator("SR Fade Long", GraphType.PRIMARY);
        fadeShort = api.registerIndicator("SR Fade Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 210, 60));
        scoreIndicator.setColor(new Color(100, 180, 255));
        levelHigh.setColor(new Color(200, 80, 80));
        levelLow.setColor(new Color(40, 160, 90));
        runLong.setColor(new Color(0, 220, 100));
        runShort.setColor(new Color(255, 50, 50));
        fadeLong.setColor(new Color(120, 220, 160));
        fadeShort.setColor(new Color(240, 150, 150));
        levelHigh.setWidth(1);
        levelLow.setWidth(1);
        runLong.setWidth(3);
        runShort.setWidth(3);
        fadeLong.setWidth(2);
        fadeShort.setWidth(2);

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(scoreIndicator);

        Log.info(String.format(
                Locale.US,
                "Stop Run init alias=%s role=%s pips=%.4f pierce=%.0f windowMs=%.0f "
                        + "minVol=%.0f minImb=%.2f minScore=%.0f pullaway=%.1f",
                alias, role, pips,
                pierceTicks, windowMs, minVol, minImbalance, minScore, pullawayPts));
    }

    private StopRunTracker buildTracker() {
        return new StopRunTracker(
                (int) Math.round(pierceTicks == null ? 2 : pierceTicks),
                (int) Math.round(windowMs == null ? 2000 : windowMs),
                minVol == null ? 25.0 : minVol,
                (int) Math.round(minPrints == null ? 8 : minPrints),
                minImbalance == null ? 0.60 : minImbalance,
                (int) Math.round(minSweeps == null ? 1 : minSweeps),
                2, // sweepMinSpanTicks
                3, // sweepMinPrints
                (int) Math.round(minAggressors == null ? 3 : minAggressors),
                (int) Math.round(minScore == null ? 2 : minScore),
                4, // mboRadiusTicks
                1000, // mboPreMs
                0.60, // mboMinPullRatio
                (int) Math.round(syncMs == null ? 1500 : syncMs),
                (int) Math.round((cooldownSec == null ? 5.0 : cooldownSec) * 1000),
                reclaimSec == null ? 5.0 : reclaimSec,
                lookbackSec == null ? 600.0 : lookbackSec,
                pullawayPts == null ? 20.0 : pullawayPts,
                5.0, // invalidateBeyondPts
                pips,
                Boolean.TRUE.equals(useSwings),
                Boolean.TRUE.equals(useSession),
                Boolean.TRUE.equals(useOvernight),
                LocalTime.of(14, 30));
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : info.symbol;
        String role = InstrumentClassifier.classify(info);
        roleByAlias.put(alias, role);
        if ("NQ".equals(role) && nqAlias == null) {
            nqAlias = alias;
        }
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        roleByAlias.computeIfAbsent(alias, a -> "OTHER");
    }

    @Override
    public void onTimestamp(long t) {
        nowNs = t;
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        if (tracker == null || size <= 0) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        int tick;
        if (price >= 20_000.0) {
            tick = (int) Math.round(price);
        } else {
            tick = PriceTicks.toTicks(price, pips);
        }
        long t = nowNs > 0 ? nowNs : System.nanoTime();
        boolean buy = tradeInfo.isBidAggressor;
        String aid = tradeInfo.aggressorOrderId;

        if ("ES".equals(role)) {
            tracker.onEsTrade(t, tick, size, buy);
            return;
        }
        // Primary / NQ
        if (!"NQ".equals(role) && currentAlias != null && !currentAlias.equals(primaryAlias)
                && !currentAlias.equals(nqAlias)) {
            return;
        }
        tracker.onTrade(t, tick, size, buy, aid);

        Signal sig = tracker.pollSignal();
        if (sig != null) {
            onFire(sig);
        }
        Resolve res = tracker.pollResolve();
        if (res != null) {
            onResolve(res);
        }
    }

    @Override
    public void send(String orderId, boolean isBid, int price, int size) {
        if (tracker == null) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        if ("ES".equals(role)) {
            return;
        }
        long t = nowNs > 0 ? nowNs : System.nanoTime();
        tracker.onMboSend(t, orderId, isBid, price, size);
    }

    @Override
    public void replace(String orderId, int price, int size) {
        if (tracker == null) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        if ("ES".equals(role)) {
            return;
        }
        long t = nowNs > 0 ? nowNs : System.nanoTime();
        tracker.onMboReplace(t, orderId, price, size);
    }

    @Override
    public void cancel(String orderId) {
        if (tracker == null) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        if ("ES".equals(role)) {
            return;
        }
        long t = nowNs > 0 ? nowNs : System.nanoTime();
        tracker.onMboCancel(t, orderId);
    }

    private void onFire(Signal sig) {
        boolean wantLong = sig.side == Side.LONG && Boolean.TRUE.equals(showLongs);
        boolean wantShort = sig.side == Side.SHORT && Boolean.TRUE.equals(showShorts);
        if (!wantLong && !wantShort) {
            return;
        }
        double chart = tickToChart(sig.triggerTick);
        lastSignalSide = sig.side == Side.LONG ? 1 : -1;
        signalHold = 20;
        lastScore = sig.score;
        if (sig.side == Side.LONG) {
            lastRunLong = chart;
            lastRunShort = Double.NaN;
        } else {
            lastRunShort = chart;
            lastRunLong = Double.NaN;
        }
        Log.info(String.format(
                Locale.US,
                "SR RUN %s kind=%s level=%d trigger=%d score=%d sideVol=%.0f imb=%.2f "
                        + "prints=%d aggr=%d sweeps=%d pen=%.1f pull=%.2f mboPull=%s "
                        + "esConf=%s esLagMs=%.0f chart=%.2f",
                sig.side,
                sig.levelKind,
                sig.levelTick,
                sig.triggerTick,
                sig.score,
                sig.sideVol,
                sig.imbalance,
                sig.nPrints,
                sig.nAggressors,
                sig.nSweeps,
                sig.maxPenetrationTicks,
                sig.pullRatio,
                sig.mboPull,
                sig.esConfirmed,
                sig.esLagMs,
                chart));
    }

    private void onResolve(Resolve res) {
        double chart = tickToChart(res.levelTick);
        if ("stall".equals(res.outcome)) {
            // Fade marker: opposite of the run
            if (res.side == Side.LONG && Boolean.TRUE.equals(showShorts)) {
                lastFadeShort = chart;
            } else if (res.side == Side.SHORT && Boolean.TRUE.equals(showLongs)) {
                lastFadeLong = chart;
            }
        }
        Log.info(String.format(
                Locale.US,
                "SR RESOLVE %s %s level=%d chart=%.2f",
                res.outcome, res.side, res.levelTick, chart));
    }

    private double tickToChart(int tick) {
        if (pips <= 0) {
            return tick;
        }
        return tick * pips;
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        if (signalIndicator == null || tracker == null) {
            return;
        }
        signalIndicator.addPoint(lastSignalSide);
        if (signalHold > 0) {
            signalHold--;
            if (signalHold == 0) {
                lastSignalSide = 0;
            }
        }
        scoreIndicator.addPoint(lastScore);

        double hi = Double.NaN;
        double lo = Double.NaN;
        if (tracker.hasActiveHigh()) {
            hi = tickToChart(tracker.activeLevelHighTick());
        }
        if (tracker.hasActiveLow()) {
            lo = tickToChart(tracker.activeLevelLowTick());
        }
        levelHigh.addPoint(hi);
        levelLow.addPoint(lo);
        runLong.addPoint(lastRunLong);
        runShort.addPoint(lastRunShort);
        fadeLong.addPoint(lastFadeLong);
        fadeShort.addPoint(lastFadeShort);
    }

    @Override
    public void stop() {
        // no-op
    }
}
