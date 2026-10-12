package com.bookmap.orderflow.effort;

import java.awt.Color;
import java.util.ArrayDeque;
import java.util.Deque;

import com.bookmap.orderflow.core.FlowBucket;
import com.bookmap.orderflow.core.PriceProgressTracker;
import com.bookmap.orderflow.core.PriceTicks;

import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
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
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Effort vs result: aggressive volume divided by price progress in ticks.
 * Rising effort with collapsing progress marks absorption / exhaustion.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Effort vs Result")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class EffortResultModule implements
        CustomModule,
        TradeDataListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Window ms", step = 50.0)
    Double windowMs = 1000.0;

    @Parameter(name = "Lookback windows", step = 1.0)
    Double lookback = 8.0;

    @Parameter(name = "Epsilon ticks", step = 0.1)
    Double epsilon = 0.5;

    @Parameter(name = "Trend lookback windows", step = 1.0)
    Double trendLookback = 5.0;

    private final FlowBucket bucket = new FlowBucket();
    private final PriceProgressTracker progress = new PriceProgressTracker();
    private final Deque<Sample> history = new ArrayDeque<>();

    private double pips = 0.25;
    private long intervalNs = Intervals.INTERVAL_1_SECOND;

    private Indicator buyEffortIndicator;
    private Indicator sellEffortIndicator;
    private Indicator buyAccelIndicator;
    private Indicator sellAccelIndicator;
    private Indicator exhaustionFlagIndicator;

    private static final class Sample {
        final double buyEffort;
        final double sellEffort;
        final long buyVol;
        final long sellVol;
        final int upTicks;
        final int downTicks;
        final int netTicks;

        Sample(double buyEffort, double sellEffort, long buyVol, long sellVol,
               int upTicks, int downTicks, int netTicks) {
            this.buyEffort = buyEffort;
            this.sellEffort = sellEffort;
            this.buyVol = buyVol;
            this.sellVol = sellVol;
            this.upTicks = upTicks;
            this.downTicks = downTicks;
            this.netTicks = netTicks;
        }
    }

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        pips = info.pips;
        intervalNs = windowMs != null && windowMs <= 500
                ? Intervals.INTERVAL_500_MILLISECONDS
                : Intervals.INTERVAL_1_SECOND;

        buyEffortIndicator = api.registerIndicator("ER Buy Effort", GraphType.BOTTOM);
        sellEffortIndicator = api.registerIndicator("ER Sell Effort", GraphType.BOTTOM);
        buyAccelIndicator = api.registerIndicator("ER Buy Effort Accel", GraphType.BOTTOM);
        sellAccelIndicator = api.registerIndicator("ER Sell Effort Accel", GraphType.BOTTOM);
        exhaustionFlagIndicator = api.registerIndicator("ER Exhaustion", GraphType.BOTTOM);

        buyEffortIndicator.setColor(new Color(0, 170, 90));
        sellEffortIndicator.setColor(new Color(210, 70, 70));
        buyAccelIndicator.setColor(new Color(120, 220, 160));
        sellAccelIndicator.setColor(new Color(240, 150, 150));
        exhaustionFlagIndicator.setColor(new Color(255, 200, 40));

        AxisGroup effortGroup = new AxisGroup();
        effortGroup.add(buyEffortIndicator);
        effortGroup.add(sellEffortIndicator);
        effortGroup.add(buyAccelIndicator);
        effortGroup.add(sellAccelIndicator);

        AxisGroup flagGroup = new AxisGroup();
        flagGroup.add(exhaustionFlagIndicator);
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        int ticks = PriceTicks.toTicks(price, pips);
        progress.onTradePrice(ticks);
        bucket.onTrade(tradeInfo.isBidAggressor, size, tradeInfo.aggressorOrderId);
    }

    @Override
    public long getInterval() {
        return intervalNs;
    }

    @Override
    public void onInterval() {
        double eps = epsilon == null || epsilon < 0.01 ? 0.5 : epsilon;
        int up = progress.upwardProgressTicks();
        int down = progress.downwardProgressTicks();
        long buyVol = bucket.getBuyVolume();
        long sellVol = bucket.getSellVolume();

        double buyEffort = buyVol / Math.max(up, eps);
        double sellEffort = sellVol / Math.max(down, eps);
        int net = progress.netProgressTicks();

        Sample sample = new Sample(buyEffort, sellEffort, buyVol, sellVol, up, down, net);
        history.addLast(sample);
        int keep = Math.max(2, lookback == null ? 8 : lookback.intValue());
        while (history.size() > keep) {
            history.removeFirst();
        }

        double buyAccel = 0;
        double sellAccel = 0;
        if (history.size() >= 2) {
            Sample prev = null;
            for (Sample s : history) {
                prev = s;
            }
            // prev is last; get second-to-last
            Sample[] arr = history.toArray(new Sample[0]);
            Sample previous = arr[arr.length - 2];
            buyAccel = sample.buyEffort - previous.buyEffort;
            sellAccel = sample.sellEffort - previous.sellEffort;
        }

        buyEffortIndicator.addPoint(buyEffort);
        sellEffortIndicator.addPoint(sellEffort);
        buyAccelIndicator.addPoint(buyAccel);
        sellAccelIndicator.addPoint(sellAccel);
        exhaustionFlagIndicator.addPoint(detectExhaustion(sample) ? 1.0 : 0.0);

        bucket.reset();
        progress.resetWindowKeepLast();
    }

    /**
     * Trend-side volume up and progress down over recent windows => exhaustion.
     */
    private boolean detectExhaustion(Sample latest) {
        int n = Math.max(2, trendLookback == null ? 5 : trendLookback.intValue());
        if (history.size() < n) {
            return false;
        }
        Sample[] arr = history.toArray(new Sample[0]);
        int start = arr.length - n;
        int netSum = 0;
        long buySumEarly = 0;
        long buySumLate = 0;
        long sellSumEarly = 0;
        long sellSumLate = 0;
        int upEarly = 0;
        int upLate = 0;
        int downEarly = 0;
        int downLate = 0;
        int half = n / 2;
        for (int i = start; i < arr.length; i++) {
            Sample s = arr[i];
            netSum += s.netTicks;
            int idx = i - start;
            if (idx < half) {
                buySumEarly += s.buyVol;
                sellSumEarly += s.sellVol;
                upEarly += s.upTicks;
                downEarly += s.downTicks;
            } else {
                buySumLate += s.buyVol;
                sellSumLate += s.sellVol;
                upLate += s.upTicks;
                downLate += s.downTicks;
            }
        }

        boolean downTrend = netSum < 0;
        boolean upTrend = netSum > 0;

        if (downTrend) {
            return sellSumLate > sellSumEarly && downLate <= downEarly
                    && latest.sellEffort > latest.buyEffort;
        }
        if (upTrend) {
            return buySumLate > buySumEarly && upLate <= upEarly
                    && latest.buyEffort > latest.sellEffort;
        }
        return false;
    }

    @Override
    public void stop() {
        bucket.reset();
        progress.clear();
        history.clear();
    }
}
