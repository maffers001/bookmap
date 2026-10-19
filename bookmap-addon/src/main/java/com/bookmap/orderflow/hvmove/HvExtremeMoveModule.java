package com.bookmap.orderflow.hvmove;

import java.awt.Color;

import com.bookmap.orderflow.core.HvExtremeMoveTracker;
import com.bookmap.orderflow.core.HvExtremeMoveTracker.Side;
import com.bookmap.orderflow.core.HvExtremeMoveTracker.Signal;
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
 * Simple HV extreme move: mark when price is N pts away from a high-volume
 * peak (short) or trough (long).
 *
 * <ul>
 *   <li>{@code HV Ext Peak/Trough} — extreme price while watching</li>
 *   <li>{@code HV Ext Target} — extreme ± Move pts while watching</li>
 *   <li>{@code HV Ext Fire Long/Short} — marker at the HV tip (trough/peak) when the move completes</li>
 *   <li>{@code HV Ext Armed} — +1 trough / −1 peak / 0 idle</li>
 *   <li>{@code HV Ext Signal} — brief fire pulse</li>
 *   <li>{@code HV Ext Vol ratio} — tip band vol ÷ (avg × band width)</li>
 * </ul>
 */
@Layer1SimpleAttachable
@Layer1StrategyName("HV Extreme Move tip")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class HvExtremeMoveModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Move pts", step = 0.25)
    Double movePts = 20.0;

    @Parameter(name = "Confirm pts", step = 0.25)
    Double confirmPts = 5.0;

    @Parameter(name = "Peak band pts", step = 0.25)
    Double peakBandPts = 2.0;

    @Parameter(name = "High vol mult", step = 0.1)
    Double highVolMult = 2.0;

    @Parameter(name = "Profile lookback min", step = 1.0)
    Double lookbackMin = 15.0;

    @Parameter(name = "Cooldown sec", step = 5.0)
    Double cooldownSec = 30.0;

    @Parameter(name = "Show longs", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts", step = 1.0)
    Boolean showShorts = true;

    @Parameter(name = "Show signal", step = 1.0)
    Boolean showSignal = true;

    @Parameter(name = "Show armed", step = 1.0)
    Boolean showArmed = true;

    @Parameter(name = "Show vol ratio", step = 1.0)
    Boolean showVolRatio = false;

    @Parameter(name = "Show in-play lines", step = 1.0)
    Boolean showInPlay = true;

    private double pips = 0.25;
    private long nowNs;
    private HvExtremeMoveTracker tracker;

    private Indicator signalIndicator;
    private Indicator armedIndicator;
    private Indicator volRatioIndicator;
    private Indicator peakLine;
    private Indicator troughLine;
    private Indicator targetLine;
    private Indicator fireLong;
    private Indicator fireShort;

    private int lastSignalSide;
    private int signalHold;
    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private Side lastArmedLog = Side.NONE;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        pips = info.pips > 0 ? info.pips : 0.25;
        tracker = buildTracker();

        signalIndicator = api.registerIndicator("HV Ext Signal", GraphType.BOTTOM);
        armedIndicator = api.registerIndicator("HV Ext Armed", GraphType.BOTTOM);
        volRatioIndicator = api.registerIndicator("HV Ext Vol ratio", GraphType.BOTTOM);

        peakLine = api.registerIndicator("HV Ext Peak", GraphType.PRIMARY);
        troughLine = api.registerIndicator("HV Ext Trough", GraphType.PRIMARY);
        targetLine = api.registerIndicator("HV Ext Target", GraphType.PRIMARY);
        fireLong = api.registerIndicator("HV Ext Fire Long", GraphType.PRIMARY);
        fireShort = api.registerIndicator("HV Ext Fire Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 210, 60));
        armedIndicator.setColor(new Color(255, 150, 40));
        volRatioIndicator.setColor(new Color(120, 180, 255));

        peakLine.setColor(new Color(220, 80, 80));
        troughLine.setColor(new Color(40, 170, 100));
        targetLine.setColor(new Color(200, 200, 80));
        fireLong.setColor(new Color(0, 230, 110));
        fireShort.setColor(new Color(255, 40, 40));
        peakLine.setWidth(2);
        troughLine.setWidth(2);
        targetLine.setWidth(1);
        fireLong.setWidth(3);
        fireShort.setWidth(3);

        // Signal/Armed share ±1 scale; Vol ratio stays on its own axis.
        AxisGroup signalAxis = new AxisGroup();
        signalAxis.add(signalIndicator);
        signalAxis.add(armedIndicator);

        Log.info(String.format(
                "HVEXT tipfix3 init alias=%s pips=%.4f movePts=%.2f confirmPts=%.2f "
                        + "bandPts=%.2f highMult=%.2f lookbackMin=%.0f (PRIMARY=tick index)",
                alias,
                pips,
                movePts,
                confirmPts,
                peakBandPts,
                highVolMult,
                lookbackMin));
    }

    private HvExtremeMoveTracker buildTracker() {
        int move = Math.max(1, ptsToTicks(movePts == null ? 20.0 : movePts));
        int confirm = Math.max(1, ptsToTicks(confirmPts == null ? 5.0 : confirmPts));
        int band = Math.max(0, ptsToTicks(peakBandPts == null ? 2.0 : peakBandPts));
        double mult = highVolMult == null ? 2.0 : highVolMult;
        long lookbackNs = (long) ((lookbackMin == null ? 15.0 : lookbackMin) * 60_000_000_000L);
        long coolNs = (long) ((cooldownSec == null ? 30.0 : cooldownSec) * 1_000_000_000L);
        return new HvExtremeMoveTracker(move, confirm, band, mult, lookbackNs, coolNs);
    }

    private int ptsToTicks(double pts) {
        if (pips <= 0) {
            return (int) Math.round(pts);
        }
        return (int) Math.round(pts / pips);
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
        int tick = PriceTicks.apiPriceToTick(price, pips);
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

        // PRIMARY indicators on this install take API tick indices (same as
        // onTrade price). Passing chart points (ticks×pips) gets ×pips again
        // in the UI (29851 → 7462) and plots off-chart.
        double tipChart = PriceTicks.toChartPoints(sig.extremeTick, pips);
        lastSignalSide = sig.side == Side.LONG ? 1 : -1;
        signalHold = 20;
        if (sig.side == Side.LONG) {
            lastFireLong = sig.extremeTick;
            lastFireShort = Double.NaN;
        } else {
            lastFireShort = sig.extremeTick;
            lastFireLong = Double.NaN;
        }

        Log.info(String.format(
                "HV EXT FIRE %s extreme=%d tipChart=%.2f primaryTick=%d trigger=%d move=%d "
                        + "vol=%.0f avg/tick=%.2f ratio=%.2f",
                sig.side,
                sig.extremeTick,
                tipChart,
                sig.extremeTick,
                sig.triggerTick,
                sig.moveTicks,
                sig.extremeVol,
                sig.avgVolPerTick,
                sig.volRatio));
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

        if (Boolean.TRUE.equals(showSignal)) {
            signalIndicator.addPoint(lastSignalSide);
        } else {
            signalIndicator.addPoint(Double.NaN);
        }
        if (signalHold > 0) {
            signalHold--;
            if (signalHold == 0) {
                lastSignalSide = 0;
            }
        }

        Side watching = tracker.watchingSide();
        boolean drawLong = Boolean.TRUE.equals(showLongs);
        boolean drawShort = Boolean.TRUE.equals(showShorts);
        boolean drawInPlay = Boolean.TRUE.equals(showInPlay);

        double armed = 0;
        double peak = Double.NaN;
        double trough = Double.NaN;
        double target = Double.NaN;

        if (watching == Side.SHORT && drawShort) {
            armed = -1;
            if (drawInPlay) {
                peak = tracker.extremeTick();
                target = tracker.extremeTick() - tracker.moveTicks();
            }
        } else if (watching == Side.LONG && drawLong) {
            armed = 1;
            if (drawInPlay) {
                trough = tracker.extremeTick();
                target = tracker.extremeTick() + tracker.moveTicks();
            }
        }

        if (watching != lastArmedLog) {
            if (watching != Side.NONE) {
                Log.info(String.format(
                        "HV EXT WATCH %s extreme=%d target=%d volRatio=%.2f",
                        watching,
                        tracker.extremeTick(),
                        watching == Side.LONG
                                ? tracker.extremeTick() + tracker.moveTicks()
                                : tracker.extremeTick() - tracker.moveTicks(),
                        tracker.currentExtremeVolRatio()));
            } else if (lastArmedLog != Side.NONE) {
                Log.info("HV EXT CLEAR was=" + lastArmedLog);
            }
            lastArmedLog = watching;
        }

        if (Boolean.TRUE.equals(showArmed)) {
            armedIndicator.addPoint(armed);
        } else {
            armedIndicator.addPoint(Double.NaN);
        }

        if (Boolean.TRUE.equals(showVolRatio)) {
            volRatioIndicator.addPoint(tracker.currentExtremeVolRatio());
        } else {
            volRatioIndicator.addPoint(Double.NaN);
        }

        peakLine.addPoint(peak);
        troughLine.addPoint(trough);
        targetLine.addPoint(target);
        fireLong.addPoint(lastFireLong);
        fireShort.addPoint(lastFireShort);
    }

    @Override
    public void stop() {
        // no-op
    }
}
