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
     * Map an API price to instrument chart points.
     *
     * <p>Tape {@code onTrade} prices on this install are usually tick indices
     * (NQ ~1e5+, modern ES ~8e4+). Some paths already supply chart points
     * (NQ/ES ~2e4–4e4). Threshold {@code 80_000} separates them: multiplying
     * chart points by pips (e.g. 29824→7456) plots PRIMARY markers off-chart.
     */
    public static double toChartPoints(double apiPrice, double pips) {
        if (pips <= 0) {
            return apiPrice;
        }
        if (apiPrice >= 80_000.0) {
            return apiPrice * pips;
        }
        return apiPrice;
    }

    /**
     * Normalize an API trade price to a tick index for trackers.
     * Tick indices ({@code >= 80_000}) pass through; chart points become
     * {@code round(price / pips)}.
     */
    public static int apiPriceToTick(double apiPrice, double pips) {
        if (apiPrice >= 80_000.0 || pips <= 0) {
            return (int) Math.round(apiPrice);
        }
        return (int) Math.round(apiPrice / pips);
    }

    /** Inverse of {@link #toChartPoints} when converting point thresholds into API units. */
    public static double pointsToApi(double pricePoints, double pips, boolean apiPriceInTicks) {
        if (!apiPriceInTicks || pips <= 0) {
            return pricePoints;
        }
        return pricePoints / pips;
    }
}
