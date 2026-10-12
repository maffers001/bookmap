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
 * Simple HV extreme move: mark when price is N ticks away from a high-volume
 * peak (short) or trough (long).
 *
 * <ul>
 *   <li>{@code HV Ext Peak/Trough} — extreme price while watching</li>
 *   <li>{@code HV Ext Target} — extreme ± Move ticks while watching</li>
 *   <li>{@code HV Ext Fire Long/Short} — marker when the move completes</li>
 *   <li>{@code HV Ext Armed} — +1 trough / −1 peak / 0 idle</li>
 *   <li>{@code HV Ext Signal} — brief fire pulse</li>
 * </ul>
 */
@Layer1SimpleAttachable
@Layer1StrategyName("HV Extreme Move")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class HvExtremeMoveModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Move ticks", step = 1.0)
    Double moveTicks = 20.0;

    @Parameter(name = "Confirm ticks", step = 1.0)
    Double confirmTicks = 4.0;

    @Parameter(name = "Extreme band ticks", step = 1.0)
    Double extremeBandTicks = 1.0;

    @Parameter(name = "High vol mult", step = 0.1)
    Double highVolMult = 2.0;

    @Parameter(name = "Profile lookback min", step = 1.0)
    Double lookbackMin = 15.0;

    @Parameter(name = "Cooldown cooldown sec", step = 5.0)
    Double cooldownSec = 30.0;

    @Parameter(name = "Show longs", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts", step = 1.0)
    Boolean showShorts = true;

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

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(armedIndicator);
        bottom.add(volRatioIndicator);

        Log.info(String.format(
                "HV Extreme Move init alias=%s pips=%.4f moveTicks=%.0f confirm=%.0f "
                        + "band=%.0f highMult=%.2f lookbackMin=%.0f",
                alias,
                pips,
                moveTicks,
                confirmTicks,
                extremeBandTicks,
                highVolMult,
                lookbackMin));
    }

    private HvExtremeMoveTracker buildTracker() {
        int move = moveTicks == null ? 20 : Math.max(1, moveTicks.intValue());
        int confirm = confirmTicks == null ? 4 : Math.max(1, confirmTicks.intValue());
        int band = extremeBandTicks == null ? 1 : Math.max(0, extremeBandTicks.intValue());
        double mult = highVolMult == null ? 2.0 : highVolMult;
        long lookbackNs = (long) ((lookbackMin == null ? 15.0 : lookbackMin) * 60_000_000_000L);
        long coolNs = (long) ((cooldownSec == null ? 30.0 : cooldownSec) * 1_000_000_000L);
        return new HvExtremeMoveTracker(move, confirm, band, mult, lookbackNs, coolNs);
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
        // On this install API trade price is already a tick index when large.
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
                "HV EXT FIRE %s extreme=%d trigger=%d move=%d vol=%.0f avg/tick=%.2f ratio=%.2f chart=%.2f",
                sig.side,
                sig.extremeTick,
                sig.triggerTick,
                sig.moveTicks,
                sig.extremeVol,
                sig.avgVolPerTick,
                sig.volRatio,
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

        Side watching = tracker.watchingSide();
        boolean drawLong = Boolean.TRUE.equals(showLongs);
        boolean drawShort = Boolean.TRUE.equals(showShorts);

        double armed = 0;
        double peak = Double.NaN;
        double trough = Double.NaN;
        double target = Double.NaN;

        if (watching == Side.SHORT && drawShort) {
            armed = -1;
            peak = tickToChart(tracker.extremeTick());
            target = tickToChart(tracker.extremeTick() - tracker.moveTicks());
        } else if (watching == Side.LONG && drawLong) {
            armed = 1;
            trough = tickToChart(tracker.extremeTick());
            target = tickToChart(tracker.extremeTick() + tracker.moveTicks());
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

        armedIndicator.addPoint(armed);
        volRatioIndicator.addPoint(tracker.currentExtremeVolRatio());
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
