package com.bookmap.orderflow.liquidity;

import java.awt.Color;

import com.bookmap.orderflow.core.LargeLevelTracker;
import com.bookmap.orderflow.core.LargeLevelTracker.EventLabel;
import com.bookmap.orderflow.core.LargeLevelTracker.TrackedLevel;
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
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Tracks large near-price resting liquidity: attract / repel / absorb /
 * withdraw / dwell-support. Bottom panel scores + primary markers at level price.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Liquidity Reaction")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class LiquidityReactionModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Large size threshold", step = 1.0)
    Double largeSize = 100.0;

    @Parameter(name = "Z-score threshold", step = 0.1)
    Double zScore = 2.5;

    @Parameter(name = "Tick radius", step = 1.0)
    Double tickRadius = 8.0;

    @Parameter(name = "Dwell alert seconds", step = 0.5)
    Double dwellAlertSeconds = 3.0;

    private LargeLevelTracker tracker;
    private double pips = 0.25;

    private Indicator absorbScoreIndicator;
    private Indicator dwellIndicator;
    private Indicator eventCodeIndicator;
    private Indicator markerBidIndicator;
    private Indicator markerAskIndicator;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        pips = info.pips;
        tracker = new LargeLevelTracker(
                largeSize == null ? 100 : largeSize.intValue(),
                zScore == null ? 2.5 : zScore,
                tickRadius == null ? 8 : tickRadius.intValue(),
                dwellAlertSeconds == null ? 3.0 : dwellAlertSeconds,
                200);

        absorbScoreIndicator = api.registerIndicator("LR Absorb Score", GraphType.BOTTOM);
        dwellIndicator = api.registerIndicator("LR Dwell Sec", GraphType.BOTTOM);
        eventCodeIndicator = api.registerIndicator("LR Event Code", GraphType.BOTTOM);
        markerBidIndicator = api.registerIndicator("LR Bid Level", GraphType.PRIMARY);
        markerAskIndicator = api.registerIndicator("LR Ask Level", GraphType.PRIMARY);

        absorbScoreIndicator.setColor(new Color(180, 120, 255));
        dwellIndicator.setColor(new Color(255, 180, 60));
        eventCodeIndicator.setColor(new Color(200, 200, 200));
        markerBidIndicator.setColor(new Color(0, 160, 255));
        markerAskIndicator.setColor(new Color(255, 100, 60));
        markerBidIndicator.setWidth(2);
        markerAskIndicator.setWidth(2);

        AxisGroup bottom = new AxisGroup();
        bottom.add(absorbScoreIndicator);
        bottom.add(dwellIndicator);
        bottom.add(eventCodeIndicator);
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        tracker.onTimestamp(nanoseconds);
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        tracker.onDepth(isBid, price, size);
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        tracker.onTrade(PriceTicks.toTicks(price, pips), size, tradeInfo.isBidAggressor);
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_250_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        absorbScoreIndicator.addPoint(tracker.currentAbsorbScore());
        dwellIndicator.addPoint(tracker.currentMaxDwellSeconds());

        double eventCode = 0;
        Double bidMark = Double.NaN;
        Double askMark = Double.NaN;

        for (TrackedLevel level : tracker.getActiveLevels()) {
            if (level.label != EventLabel.NONE) {
                eventCode = Math.max(eventCode, tracker.eventCode(level.label));
            }
            double displayPrice = level.price * pips;
            if (level.side == LargeLevelTracker.Side.BID) {
                bidMark = displayPrice;
            } else {
                askMark = displayPrice;
            }
        }

        for (TrackedLevel closed : tracker.drainClosedEvents()) {
            eventCode = Math.max(eventCode, tracker.eventCode(closed.label));
            Log.info(String.format(
                    "LR %s @%d size=%d exec=%d replenish=%d pen=%d label=%s age=%.1fs",
                    closed.side,
                    closed.price, closed.initialSize, closed.executedInto,
                    closed.replenishCount, closed.maxPenetrationTicks,
                    closed.label, closed.ageSeconds(closed.lastUpdateNs)));
            double displayPrice = closed.price * pips;
            if (closed.side == LargeLevelTracker.Side.BID) {
                bidMark = displayPrice;
            } else {
                askMark = displayPrice;
            }
        }

        eventCodeIndicator.addPoint(eventCode);
        markerBidIndicator.addPoint(bidMark);
        markerAskIndicator.addPoint(askMark);
    }

    @Override
    public void stop() {
        // nothing retained across reloads
    }
}
