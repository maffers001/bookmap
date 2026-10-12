package com.bookmap.orderflow.flow;

import java.awt.Color;

import com.bookmap.orderflow.core.Ema;
import com.bookmap.orderflow.core.FlowBucket;

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
 * Aggressive buy/sell volume, order estimates, average size, and delta.
 * Toggle which series plot; optionally plot AF Sell as a positive value.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Aggressive Flow")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class AggressiveFlowModule implements
        CustomModule,
        TradeDataListener,
        IntervalListener,
        HistoricalDataListener {

    @Parameter(name = "Window ms (250 or 1000)", step = 50.0)
    Double windowMs = 1000.0;

    @Parameter(name = "Use EMA smoothing")
    Boolean useEma = true;

    @Parameter(name = "EMA periods", step = 1.0)
    Double emaPeriods = 5.0;

    @Parameter(name = "Show order counts instead of volume")
    Boolean showOrders = false;

    @Parameter(name = "Plot AF Buy")
    Boolean plotBuy = true;

    @Parameter(name = "Plot AF Sell")
    Boolean plotSell = true;

    @Parameter(name = "Plot AF Delta")
    Boolean plotDelta = true;

    @Parameter(name = "Plot AF Avg Buy Size")
    Boolean plotAvgBuySize = false;

    @Parameter(name = "Plot AF Avg Sell Size")
    Boolean plotAvgSellSize = false;

    @Parameter(name = "AF Sell as positive")
    Boolean sellAsPositive = false;

    private final FlowBucket bucket = new FlowBucket();
    private Ema buyEma;
    private Ema sellEma;
    private Ema deltaEma;

    private Indicator buyIndicator;
    private Indicator sellIndicator;
    private Indicator deltaIndicator;
    private Indicator avgBuySizeIndicator;
    private Indicator avgSellSizeIndicator;

    private long intervalNs = Intervals.INTERVAL_1_SECOND;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        intervalNs = resolveIntervalNs(windowMs);
        int periods = Math.max(1, emaPeriods == null ? 5 : emaPeriods.intValue());
        buyEma = new Ema(periods);
        sellEma = new Ema(periods);
        deltaEma = new Ema(periods);

        buyIndicator = api.registerIndicator("AF Buy", GraphType.BOTTOM);
        sellIndicator = api.registerIndicator("AF Sell", GraphType.BOTTOM);
        deltaIndicator = api.registerIndicator("AF Delta", GraphType.BOTTOM);
        avgBuySizeIndicator = api.registerIndicator("AF Avg Buy Size", GraphType.BOTTOM);
        avgSellSizeIndicator = api.registerIndicator("AF Avg Sell Size", GraphType.BOTTOM);

        buyIndicator.setColor(new Color(0, 180, 80));
        sellIndicator.setColor(new Color(220, 60, 60));
        deltaIndicator.setColor(new Color(80, 140, 220));
        avgBuySizeIndicator.setColor(new Color(0, 120, 60));
        avgSellSizeIndicator.setColor(new Color(160, 40, 40));

        AxisGroup volumeGroup = new AxisGroup();
        volumeGroup.add(buyIndicator);
        volumeGroup.add(sellIndicator);
        volumeGroup.add(deltaIndicator);

        AxisGroup sizeGroup = new AxisGroup();
        sizeGroup.add(avgBuySizeIndicator);
        sizeGroup.add(avgSellSizeIndicator);
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        // isBidAggressor true => buyer lifted the offer (aggressive buy)
        boolean buyAggressor = tradeInfo.isBidAggressor;
        bucket.onTrade(buyAggressor, size, tradeInfo.aggressorOrderId);
    }

    @Override
    public long getInterval() {
        return intervalNs;
    }

    @Override
    public void onInterval() {
        boolean orders = showOrders != null && showOrders;
        double buyRaw = orders ? bucket.getBuyOrders() : bucket.getBuyVolume();
        double sellRaw = orders ? bucket.getSellOrders() : bucket.getSellVolume();
        double deltaRaw = orders ? bucket.getOrderDelta() : bucket.getVolumeDelta();
        double avgBuy = bucket.getAvgBuyOrderSize();
        double avgSell = bucket.getAvgSellOrderSize();

        boolean smooth = useEma == null || useEma;
        double buy = smooth ? buyEma.update(buyRaw) : buyRaw;
        double sell = smooth ? sellEma.update(sellRaw) : sellRaw;
        double delta = smooth ? deltaEma.update(deltaRaw) : deltaRaw;

        boolean positiveSell = sellAsPositive != null && sellAsPositive;
        double sellPlot = positiveSell ? sell : -sell;

        buyIndicator.addPoint(isOn(plotBuy) ? buy : Double.NaN);
        sellIndicator.addPoint(isOn(plotSell) ? sellPlot : Double.NaN);
        deltaIndicator.addPoint(isOn(plotDelta) ? delta : Double.NaN);
        avgBuySizeIndicator.addPoint(isOn(plotAvgBuySize) ? avgBuy : Double.NaN);
        avgSellSizeIndicator.addPoint(isOn(plotAvgSellSize) ? avgSell : Double.NaN);

        bucket.reset();
    }

    private static boolean isOn(Boolean flag) {
        return flag == null || flag;
    }

    @Override
    public void stop() {
        bucket.reset();
    }

    private static long resolveIntervalNs(Double windowMs) {
        long ms = windowMs == null ? 1000L : Math.round(windowMs);
        if (ms <= 100) {
            return Intervals.INTERVAL_100_MILLISECONDS;
        }
        if (ms <= 250) {
            return Intervals.INTERVAL_250_MILLISECONDS;
        }
        if (ms <= 500) {
            return Intervals.INTERVAL_500_MILLISECONDS;
        }
        return Intervals.INTERVAL_1_SECOND;
    }
}
