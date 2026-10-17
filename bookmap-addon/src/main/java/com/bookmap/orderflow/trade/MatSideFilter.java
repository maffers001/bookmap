package com.bookmap.orderflow.trade;

import java.util.concurrent.atomic.AtomicBoolean;

import velox.api.layer1.common.Log;

/**
 * Process-wide side filter for Momentum AutoTrade.
 * Updated by {@link MatSideHotkeys} / {@link MomentumAutoTradeHotkeys} and
 * seeded/synced from the MAT panel Allow longs/shorts parameters.
 */
public final class MatSideFilter {

    private static final AtomicBoolean ALLOW_LONGS = new AtomicBoolean(true);
    private static final AtomicBoolean ALLOW_SHORTS = new AtomicBoolean(true);

    private MatSideFilter() {}

    public static boolean allowLongs() {
        return ALLOW_LONGS.get();
    }

    public static boolean allowShorts() {
        return ALLOW_SHORTS.get();
    }

    public static boolean allowSide(int side) {
        return side > 0 ? allowLongs() : allowShorts();
    }

    public static void set(boolean longs, boolean shorts, String via) {
        ALLOW_LONGS.set(longs);
        ALLOW_SHORTS.set(shorts);
        Log.info("MAT side filter: " + label() + "  [longs=" + longs + " shorts=" + shorts + "]  (" + via + ")");
    }

    public static void toggleLongs() {
        set(!allowLongs(), allowShorts(), "toggled longs");
    }

    public static void toggleShorts() {
        set(allowLongs(), !allowShorts(), "toggled shorts");
    }

    /** Human-readable current mode, e.g. {@code SHORTS only}. */
    public static String label() {
        boolean L = allowLongs();
        boolean S = allowShorts();
        if (L && S) {
            return "BOTH";
        }
        if (L) {
            return "LONGS only";
        }
        if (S) {
            return "SHORTS only";
        }
        return "NONE (signals off)";
    }
}
