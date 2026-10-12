package com.bookmap.orderflow.core;

/**
 * Accumulates aggressive buy/sell activity for a fixed time window.
 */
public final class FlowBucket {
    private long buyVolume;
    private long sellVolume;
    private int buyTrades;
    private int sellTrades;
    private int buyOrders;
    private int sellOrders;
    private String lastBuyAggressorId;
    private String lastSellAggressorId;

    public void onTrade(boolean buyAggressor, int size, String aggressorOrderId) {
        if (buyAggressor) {
            buyVolume += size;
            buyTrades++;
            if (aggressorOrderId == null || !aggressorOrderId.equals(lastBuyAggressorId)) {
                buyOrders++;
                lastBuyAggressorId = aggressorOrderId;
            }
        } else {
            sellVolume += size;
            sellTrades++;
            if (aggressorOrderId == null || !aggressorOrderId.equals(lastSellAggressorId)) {
                sellOrders++;
                lastSellAggressorId = aggressorOrderId;
            }
        }
    }

    public void reset() {
        buyVolume = 0;
        sellVolume = 0;
        buyTrades = 0;
        sellTrades = 0;
        buyOrders = 0;
        sellOrders = 0;
        lastBuyAggressorId = null;
        lastSellAggressorId = null;
    }

    public long getBuyVolume() {
        return buyVolume;
    }

    public long getSellVolume() {
        return sellVolume;
    }

    public int getBuyTrades() {
        return buyTrades;
    }

    public int getSellTrades() {
        return sellTrades;
    }

    public int getBuyOrders() {
        return buyOrders;
    }

    public int getSellOrders() {
        return sellOrders;
    }

    public long getVolumeDelta() {
        return buyVolume - sellVolume;
    }

    public int getOrderDelta() {
        return buyOrders - sellOrders;
    }

    public double getAvgBuyOrderSize() {
        return buyOrders == 0 ? 0.0 : (double) buyVolume / buyOrders;
    }

    public double getAvgSellOrderSize() {
        return sellOrders == 0 ? 0.0 : (double) sellVolume / sellOrders;
    }

    public FlowSnapshot snapshot() {
        return new FlowSnapshot(
                buyVolume, sellVolume,
                buyTrades, sellTrades,
                buyOrders, sellOrders);
    }

    public static final class FlowSnapshot {
        public final long buyVolume;
        public final long sellVolume;
        public final int buyTrades;
        public final int sellTrades;
        public final int buyOrders;
        public final int sellOrders;

        public FlowSnapshot(long buyVolume, long sellVolume,
                            int buyTrades, int sellTrades,
                            int buyOrders, int sellOrders) {
            this.buyVolume = buyVolume;
            this.sellVolume = sellVolume;
            this.buyTrades = buyTrades;
            this.sellTrades = sellTrades;
            this.buyOrders = buyOrders;
            this.sellOrders = sellOrders;
        }

        public long volumeDelta() {
            return buyVolume - sellVolume;
        }
    }
}
