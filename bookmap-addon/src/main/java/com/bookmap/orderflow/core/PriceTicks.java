package com.bookmap.orderflow.core;

/** Shared helpers for mapping Bookmap prices to integer ticks / chart points. */
public final class PriceTicks {
    private PriceTicks() {}

    public static int toTicks(double price, double pips) {
        if (pips <= 0) {
            return (int) Math.round(price);
        }
        return (int) Math.round(price / pips);
    }

    /**
     * Rithmic Simplified-API trade {@code price} on this install arrives as a
     * tick index (e.g. NQ ~117000 → 29250 pts). Chart markers and orders need
     * instrument price points = ticks × pips.
     *
     * Heuristic: if {@code apiPrice} looks like a tick index (≫ typical index
     * futures points), multiply by pips; otherwise leave as points.
     */
    public static double toChartPoints(double apiPrice, double pips) {
        if (pips <= 0) {
            return apiPrice;
        }
        // NQ ticks ~1e5; ES ticks ~2e4–4e4. Chart points are typically &lt; 50k for ES/NQ.
        if (apiPrice >= 20_000.0) {
            return apiPrice * pips;
        }
        return apiPrice;
    }

    /** Inverse of {@link #toChartPoints} when converting point thresholds into API units. */
    public static double pointsToApi(double pricePoints, double pips, boolean apiPriceInTicks) {
        if (!apiPriceInTicks || pips <= 0) {
            return pricePoints;
        }
        return pricePoints / pips;
    }
}
