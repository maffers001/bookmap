package com.bookmap.orderflow.retest;

import java.awt.Color;

import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.TrappedRetestTracker;
import com.bookmap.orderflow.core.TrappedRetestTracker.Phase;
import com.bookmap.orderflow.core.TrappedRetestTracker.Side;
import com.bookmap.orderflow.core.TrappedRetestTracker.Signal;

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
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Trapped size at an HV extreme, then a weaker failed retest, then break.
 *
 * <ul>
 *   <li>{@code TR Peak1 Long/Short} — first HV extreme while in play</li>
 *   <li>{@code TR Peak2 Long/Short} — weak retest extreme while waiting for break</li>
 *   <li>{@code TR Fire Long/Short} — marker when price breaks beyond peak2</li>
 *   <li>{@code TR Phase} — 0 idle / ±1 after peak1 / +/-2 after peak2</li>
 *   <li>{@code TR Peak ratio} — peak1 band vol ÷ avg (arm quality)</li>
 *   <li>{@code TR Retest ratio} — peak2 ÷ peak1 vol (want low; ≤ Max retest frac)</li>
 *   <li>{@code TR Signal} — brief +/-1 on fire</li>
 * </ul>
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Trapped Retest")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class TrappedRetestModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Confirm pts", step = 0.25)
    Double confirmPts = 5.0;

    @Parameter(name = "Peak band pts", step = 0.25)
    Double peakBandPts = 2.0;

    @Parameter(name = "Min pullback pts", step = 0.25)
    Double minPullbackPts = 5.0;

    @Parameter(name = "Break pts", step = 0.25)
    Double breakPts = 4.0;

    @Parameter(name = "High vol mult", step = 0.1)
    Double highVolMult = 1.5;

    @Parameter(name = "Max retest frac", step = 0.05)
    Double maxRetestFrac = 0.60;

    @Parameter(name = "Profile lookback min", step = 1.0)
    Double lookbackMin = 30.0;

    @Parameter(name = "Alert cooldown sec", step = 5.0)
    Double cooldownSec = 60.0;

    @Parameter(name = "Show longs", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts", step = 1.0)
    Boolean showShorts = true;

    @Parameter(name = "Show in-play lines", step = 1.0)
    Boolean showInPlay = true;

    private double pips = 0.25;
    private long nowNs;
    private TrappedRetestTracker tracker;

    private Indicator signalIndicator;
    private Indicator phaseIndicator;
    private Indicator peakRatioIndicator;
    private Indicator retestRatioIndicator;
    private Indicator peak1Long;
    private Indicator peak1Short;
    private Indicator peak2Long;
    private Indicator peak2Short;
    private Indicator fireLong;
    private Indicator fireShort;

    private int lastSignalSide;
    private int signalHold;
    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private Phase lastLoggedPhase = Phase.IDLE;
    private Side lastLoggedSide = Side.NONE;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        pips = info.pips > 0 ? info.pips : 0.25;
        tracker = buildTracker();

        signalIndicator = api.registerIndicator("TR Signal", GraphType.BOTTOM);
        phaseIndicator = api.registerIndicator("TR Phase", GraphType.BOTTOM);
        peakRatioIndicator = api.registerIndicator("TR Peak ratio", GraphType.BOTTOM);
        retestRatioIndicator = api.registerIndicator("TR Retest ratio", GraphType.BOTTOM);

        peak1Long = api.registerIndicator("TR Peak1 Long", GraphType.PRIMARY);
        peak1Short = api.registerIndicator("TR Peak1 Short", GraphType.PRIMARY);
        peak2Long = api.registerIndicator("TR Peak2 Long", GraphType.PRIMARY);
        peak2Short = api.registerIndicator("TR Peak2 Short", GraphType.PRIMARY);
        fireLong = api.registerIndicator("TR Fire Long", GraphType.PRIMARY);
        fireShort = api.registerIndicator("TR Fire Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 210, 60));
        phaseIndicator.setColor(new Color(255, 150, 40));
        peakRatioIndicator.setColor(new Color(100, 180, 255));
        retestRatioIndicator.setColor(new Color(180, 140, 255));

        peak1Long.setColor(new Color(40, 160, 90));
        peak1Short.setColor(new Color(200, 80, 80));
        peak2Long.setColor(new Color(120, 220, 160));
        peak2Short.setColor(new Color(240, 150, 150));
        fireLong.setColor(new Color(0, 220, 100));
        fireShort.setColor(new Color(255, 50, 50));
        peak1Long.setWidth(2);
        peak1Short.setWidth(2);
        peak2Long.setWidth(2);
        peak2Short.setWidth(2);
        fireLong.setWidth(3);
        fireShort.setWidth(3);

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(phaseIndicator);
        bottom.add(peakRatioIndicator);
        bottom.add(retestRatioIndicator);

        Log.info(String.format(
                "Trapped Retest init alias=%s pips=%.4f confirm=%.2f band=%.2f "
                        + "pullback=%.2f break=%.2f highMult=%.2f maxRetest=%.2f lookbackMin=%.0f",
                alias,
                pips,
                confirmPts,
                peakBandPts,
                minPullbackPts,
                breakPts,
                highVolMult,
                maxRetestFrac,
                lookbackMin));
    }

    private TrappedRetestTracker buildTracker() {
        int confirm = ptsToTicks(confirmPts == null ? 5.0 : confirmPts);
        int band = ptsToTicks(peakBandPts == null ? 2.0 : peakBandPts);
        int pullback = ptsToTicks(minPullbackPts == null ? 5.0 : minPullbackPts);
        int brk = ptsToTicks(breakPts == null ? 4.0 : breakPts);
        double high = highVolMult == null ? 1.5 : highVolMult;
        double retest = maxRetestFrac == null ? 0.60 : maxRetestFrac;
        long lookbackNs = (long) ((lookbackMin == null ? 30.0 : lookbackMin) * 60_000_000_000L);
        long coolNs = (long) ((cooldownSec == null ? 60.0 : cooldownSec) * 1_000_000_000L);
        return new TrappedRetestTracker(
                confirm, band, pullback, brk, high, retest, lookbackNs, coolNs);
    }

    private int ptsToTicks(double pts) {
        if (pips <= 0) {
            return Math.max(1, (int) Math.round(pts));
        }
        return Math.max(1, (int) Math.round(pts / pips));
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
        int tick;
        if (price >= 20_000.0) {
            tick = (int) Math.round(price);
        } else {
            tick = PriceTicks.toTicks(price, pips);
        }
        long t = nowNs > 0 ? nowNs : System.nanoTime();
        tracker.onTrade(t, tick, size);

        Signal sig = tracker.pollSignal();
        if (sig != null) {
            onFire(sig);
        }
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
        if (sig.side == Side.LONG) {
            lastFireLong = chart;
            lastFireShort = Double.NaN;
        } else {
            lastFireShort = chart;
            lastFireLong = Double.NaN;
        }

        Log.info(String.format(
                "TR FIRE %s peak1=%d peak2=%d trigger=%d peak1Vol=%.0f peak2Vol=%.0f "
                        + "peakRatio=%.2f retestRatio=%.2f chart=%.2f",
                sig.side,
                sig.peak1Tick,
                sig.peak2Tick,
                sig.triggerTick,
                sig.peak1Vol,
                sig.peak2Vol,
                sig.peakRatio,
                sig.retestRatio,
                chart));
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

        Side s = tracker.side();
        Phase ph = tracker.phase();
        boolean drawInPlay = Boolean.TRUE.equals(showInPlay);
        boolean drawLong = Boolean.TRUE.equals(showLongs);
        boolean drawShort = Boolean.TRUE.equals(showShorts);

        double phaseCode = 0;
        double p1Long = Double.NaN;
        double p1Short = Double.NaN;
        double p2Long = Double.NaN;
        double p2Short = Double.NaN;

        if (ph != Phase.IDLE && drawInPlay) {
            double p1 = tickToChart(tracker.peak1Tick());
            if (s == Side.LONG && drawLong) {
                phaseCode = ph == Phase.AFTER_PEAK2 ? 2 : 1;
                p1Long = p1;
                if (ph == Phase.AFTER_PEAK2) {
                    p2Long = tickToChart(tracker.peak2Tick());
                }
            } else if (s == Side.SHORT && drawShort) {
                phaseCode = ph == Phase.AFTER_PEAK2 ? -2 : -1;
                p1Short = p1;
                if (ph == Phase.AFTER_PEAK2) {
                    p2Short = tickToChart(tracker.peak2Tick());
                }
            }
        }

        if (ph != lastLoggedPhase || s != lastLoggedSide) {
            if (ph != Phase.IDLE) {
                Log.info(String.format(
                        "TR %s %s peak1=%d peak2=%d peakRatio=%.2f retestRatio=%.2f",
                        ph,
                        s,
                        tracker.peak1Tick(),
                        tracker.peak2Tick(),
                        tracker.currentPeakRatio(),
                        tracker.currentRetestRatio()));
            } else if (lastLoggedPhase != Phase.IDLE) {
                Log.info("TR IDLE was=" + lastLoggedSide + "/" + lastLoggedPhase);
            }
            lastLoggedPhase = ph;
            lastLoggedSide = s;
        }

        phaseIndicator.addPoint(phaseCode);
        peakRatioIndicator.addPoint(tracker.currentPeakRatio());
        retestRatioIndicator.addPoint(tracker.currentRetestRatio());
        peak1Long.addPoint(p1Long);
        peak1Short.addPoint(p1Short);
        peak2Long.addPoint(p2Long);
        peak2Short.addPoint(p2Short);
        fireLong.addPoint(lastFireLong);
        fireShort.addPoint(lastFireShort);
    }

    @Override
    public void stop() {
        // no-op
    }
}
