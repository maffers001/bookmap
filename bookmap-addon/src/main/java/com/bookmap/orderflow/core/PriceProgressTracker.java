package com.bookmap.orderflow.core;

/**
 * Tracks price progress in ticks over a window for effort/result calculations.
 * Prices are expected in Bookmap integer tick units (price / pips).
 */
public final class PriceProgressTracker {
    private Integer windowOpenPrice;
    private Integer lastPrice;
    private int highPrice;
    private int lowPrice;

    public void onTradePrice(int priceTicks) {
        if (windowOpenPrice == null) {
            windowOpenPrice = priceTicks;
            highPrice = priceTicks;
            lowPrice = priceTicks;
        } else {
            highPrice = Math.max(highPrice, priceTicks);
            lowPrice = Math.min(lowPrice, priceTicks);
        }
        lastPrice = priceTicks;
    }

    public int upwardProgressTicks() {
        if (windowOpenPrice == null || lastPrice == null) {
            return 0;
        }
        return Math.max(0, highPrice - windowOpenPrice);
    }

    public int downwardProgressTicks() {
        if (windowOpenPrice == null || lastPrice == null) {
            return 0;
        }
        return Math.max(0, windowOpenPrice - lowPrice);
    }

    public int netProgressTicks() {
        if (windowOpenPrice == null || lastPrice == null) {
            return 0;
        }
        return lastPrice - windowOpenPrice;
    }

    public Integer getLastPrice() {
        return lastPrice;
    }

    public void resetWindowKeepLast() {
        if (lastPrice != null) {
            windowOpenPrice = lastPrice;
            highPrice = lastPrice;
            lowPrice = lastPrice;
        } else {
            windowOpenPrice = null;
        }
    }

    public void clear() {
        windowOpenPrice = null;
        lastPrice = null;
        highPrice = 0;
        lowPrice = 0;
    }
}
