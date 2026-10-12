package com.bookmap.orderflow.pitch;

import java.awt.Color;

import com.bookmap.orderflow.core.PitchAndCatchTracker;
import com.bookmap.orderflow.core.PitchAndCatchTracker.Side;
import com.bookmap.orderflow.core.PitchAndCatchTracker.Signal;
import com.bookmap.orderflow.core.PriceTicks;

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
 * Pitch and Catch alert: high volume at a rotation peak/trough, then a
 * configurable low-volume move away (default 20 pts).
 *
 * <ul>
 *   <li>{@code PC Pitch Long/Short} — pitch price while setup is armed (in play)</li>
 *   <li>{@code PC Target Long/Short} — catch level (pitch ± Catch pts) while armed</li>
 *   <li>{@code PC Fire Long/Short} — marker when the catch completes</li>
 *   <li>{@code PC Armed} — +1 long / −1 short / 0 while a setup is in play</li>
 *   <li>{@code PC Signal} — brief +1/−1 when catch fires</li>
 *   <li>{@code PC Peak/Path ratio} — live volume quality vs average</li>
 * </ul>
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Pitch and Catch")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class PitchAndCatchModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Catch pts", step = 1.0)
    Double catchPts = 20.0;

    @Parameter(name = "Peak band pts", step = 0.25)
    Double peakBandPts = 2.0;

    @Parameter(name = "Confirm pts", step = 0.25)
    Double confirmPts = 5.0;

    @Parameter(name = "Reclaim pts", step = 0.25)
    Double reclaimPts = 1.0;

    @Parameter(name = "High vol mult", step = 0.1)
    Double highVolMult = 1.5;

    @Parameter(name = "Low vol frac", step = 0.05)
    Double lowVolFrac = 0.70;

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
    private PitchAndCatchTracker tracker;

    private Indicator signalIndicator;
    private Indicator armedIndicator;
    private Indicator peakRatioIndicator;
    private Indicator pathRatioIndicator;
    private Indicator pitchLongMarker;
    private Indicator pitchShortMarker;
    private Indicator targetLongMarker;
    private Indicator targetShortMarker;
    private Indicator fireLongMarker;
    private Indicator fireShortMarker;

    private int lastSignalSide;
    private int signalHoldIntervals;
    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private Side lastLoggedArmed = Side.NONE;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        pips = info.pips > 0 ? info.pips : 0.25;
        tracker = buildTracker();

        signalIndicator = api.registerIndicator("PC Signal", GraphType.BOTTOM);
        armedIndicator = api.registerIndicator("PC Armed", GraphType.BOTTOM);
        peakRatioIndicator = api.registerIndicator("PC Peak ratio", GraphType.BOTTOM);
        pathRatioIndicator = api.registerIndicator("PC Path ratio", GraphType.BOTTOM);

        pitchLongMarker = api.registerIndicator("PC Pitch Long", GraphType.PRIMARY);
        pitchShortMarker = api.registerIndicator("PC Pitch Short", GraphType.PRIMARY);
        targetLongMarker = api.registerIndicator("PC Target Long", GraphType.PRIMARY);
        targetShortMarker = api.registerIndicator("PC Target Short", GraphType.PRIMARY);
        fireLongMarker = api.registerIndicator("PC Fire Long", GraphType.PRIMARY);
        fireShortMarker = api.registerIndicator("PC Fire Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 210, 60));
        armedIndicator.setColor(new Color(255, 160, 40));
        peakRatioIndicator.setColor(new Color(100, 180, 255));
        pathRatioIndicator.setColor(new Color(180, 140, 255));

        // In-play: softer / thinner; fire: bold
        pitchLongMarker.setColor(new Color(40, 160, 90));
        pitchShortMarker.setColor(new Color(200, 80, 80));
        targetLongMarker.setColor(new Color(120, 220, 160));
        targetShortMarker.setColor(new Color(240, 150, 150));
        fireLongMarker.setColor(new Color(0, 220, 100));
        fireShortMarker.setColor(new Color(255, 50, 50));
        pitchLongMarker.setWidth(2);
        pitchShortMarker.setWidth(2);
        targetLongMarker.setWidth(1);
        targetShortMarker.setWidth(1);
        fireLongMarker.setWidth(3);
        fireShortMarker.setWidth(3);

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(armedIndicator);
        bottom.add(peakRatioIndicator);
        bottom.add(pathRatioIndicator);

        Log.info(String.format(
                "Pitch and Catch init alias=%s pips=%.4f catchPts=%.1f peakBand=%.2f "
                        + "highMult=%.2f lowFrac=%.2f lookbackMin=%.0f inPlay=%s",
                alias,
                pips,
                catchPts,
                peakBandPts,
                highVolMult,
                lowVolFrac,
                lookbackMin,
                showInPlay));
    }

    private PitchAndCatchTracker buildTracker() {
        int catchTicks = ptsToTicks(catchPts == null ? 20.0 : catchPts);
        int peakBand = ptsToTicks(peakBandPts == null ? 2.0 : peakBandPts);
        int confirm = ptsToTicks(confirmPts == null ? 5.0 : confirmPts);
        int reclaim = ptsToTicks(reclaimPts == null ? 1.0 : reclaimPts);
        double high = highVolMult == null ? 1.5 : highVolMult;
        double low = lowVolFrac == null ? 0.70 : lowVolFrac;
        long lookbackNs = (long) ((lookbackMin == null ? 30.0 : lookbackMin) * 60_000_000_000L);
        long coolNs = (long) ((cooldownSec == null ? 60.0 : cooldownSec) * 1_000_000_000L);
        return new PitchAndCatchTracker(
                catchTicks, peakBand, confirm, reclaim, high, low, lookbackNs, coolNs);
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
            onSignal(sig, price);
        }
    }

    private void onSignal(Signal sig, double apiPrice) {
        boolean wantLong = sig.side == Side.LONG && Boolean.TRUE.equals(showLongs);
        boolean wantShort = sig.side == Side.SHORT && Boolean.TRUE.equals(showShorts);
        if (!wantLong && !wantShort) {
            return;
        }

        double chartPrice = PriceTicks.toChartPoints(apiPrice, pips);
        double catchChart = tickToChart(sig.catchTick);
        if (!Double.isNaN(catchChart)) {
            chartPrice = catchChart;
        }

        lastSignalSide = sig.side == Side.LONG ? 1 : -1;
        signalHoldIntervals = 20;
        if (sig.side == Side.LONG) {
            lastFireLong = chartPrice;
            lastFireShort = Double.NaN;
        } else {
            lastFireShort = chartPrice;
            lastFireLong = Double.NaN;
        }

        Log.info(String.format(
                "PC SIGNAL %s pitch=%d catch=%d peakVol=%.0f pathVol=%.0f "
                        + "avg/tick=%.2f peakRatio=%.2f pathRatio=%.2f chart=%.2f",
                sig.side,
                sig.pitchTick,
                sig.catchTick,
                sig.peakVol,
                sig.pathVol,
                sig.avgVolPerTick,
                sig.peakRatio,
                sig.pathRatio,
                chartPrice));
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
        if (signalHoldIntervals > 0) {
            signalHoldIntervals--;
            if (signalHoldIntervals == 0) {
                lastSignalSide = 0;
            }
        }

        Side armed = tracker.armedSide();
        boolean drawInPlay = Boolean.TRUE.equals(showInPlay);
        boolean drawLong = Boolean.TRUE.equals(showLongs);
        boolean drawShort = Boolean.TRUE.equals(showShorts);

        double armedCode = 0;
        double pitchLong = Double.NaN;
        double pitchShort = Double.NaN;
        double targetLong = Double.NaN;
        double targetShort = Double.NaN;

        if (armed != Side.NONE && drawInPlay) {
            int pitch = tracker.pitchTick();
            int catchDist = tracker.catchTicks();
            double pitchChart = tickToChart(pitch);
            if (armed == Side.LONG && drawLong) {
                armedCode = 1;
                pitchLong = pitchChart;
                targetLong = tickToChart(pitch + catchDist);
            } else if (armed == Side.SHORT && drawShort) {
                armedCode = -1;
                pitchShort = pitchChart;
                targetShort = tickToChart(pitch - catchDist);
            }
        }

        if (armed != lastLoggedArmed) {
            if (armed != Side.NONE) {
                Log.info(String.format(
                        "PC ARMED %s pitch=%d target=%d peakRatio=%.2f pathRatio=%.2f",
                        armed,
                        tracker.pitchTick(),
                        armed == Side.LONG
                                ? tracker.pitchTick() + tracker.catchTicks()
                                : tracker.pitchTick() - tracker.catchTicks(),
                        tracker.currentPeakRatio(),
                        tracker.currentPathRatio()));
            } else if (lastLoggedArmed != Side.NONE) {
                Log.info("PC DISARMED was=" + lastLoggedArmed);
            }
            lastLoggedArmed = armed;
        }

        armedIndicator.addPoint(armedCode);
        peakRatioIndicator.addPoint(tracker.currentPeakRatio());
        double pathR = tracker.currentPathRatio();
        pathRatioIndicator.addPoint(Double.isNaN(pathR) ? 0 : pathR);

        pitchLongMarker.addPoint(pitchLong);
        pitchShortMarker.addPoint(pitchShort);
        targetLongMarker.addPoint(targetLong);
        targetShortMarker.addPoint(targetShort);
        fireLongMarker.addPoint(lastFireLong);
        fireShortMarker.addPoint(lastFireShort);
    }

    @Override
    public void stop() {
        // no-op
    }
}
