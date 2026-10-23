package com.bookmap.orderflow.trade;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BiConsumer;

import velox.api.layer1.common.Log;

/**
 * Process-wide side filter for Momentum AutoTrade.
 * Updated by {@link MatSideHotkeys} / {@link MomentumAutoTradeHotkeys} /
 * {@link MatControlPanel} and seeded/synced from the MAT panel Allow longs/shorts.
 */
public final class MatSideFilter {

    private static final AtomicBoolean ALLOW_LONGS = new AtomicBoolean(true);
    private static final AtomicBoolean ALLOW_SHORTS = new AtomicBoolean(true);
    private static final List<BiConsumer<Boolean, Boolean>> LISTENERS = new CopyOnWriteArrayList<>();

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

    /** Listener receives (allowLongs, allowShorts) after every {@link #set}. */
    public static void addListener(BiConsumer<Boolean, Boolean> listener) {
        if (listener != null) {
            LISTENERS.add(listener);
        }
    }

    public static void removeListener(BiConsumer<Boolean, Boolean> listener) {
        LISTENERS.remove(listener);
    }

    public static void set(boolean longs, boolean shorts, String via) {
        ALLOW_LONGS.set(longs);
        ALLOW_SHORTS.set(shorts);
        Log.info("MAT side filter: " + label() + "  [longs=" + longs + " shorts=" + shorts + "]  (" + via + ")");
        for (BiConsumer<Boolean, Boolean> listener : LISTENERS) {
            try {
                listener.accept(longs, shorts);
            } catch (RuntimeException ex) {
                Log.info("MAT side filter listener failed: " + ex.getMessage());
            }
        }
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
