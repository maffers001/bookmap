package com.bookmap.orderflow.trade;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import velox.api.layer1.common.Log;

/**
 * Process-wide hooks for the MAT Side console (flatten / emergency actions).
 * MAT modules register a flattener while attached.
 */
public final class MatTradeControl {

    @FunctionalInterface
    public interface Flattener {
        void flatten();
    }

    private static final List<Flattener> FLATTENERS = new CopyOnWriteArrayList<>();

    private MatTradeControl() {}

    public static void register(Flattener flattener) {
        if (flattener != null) {
            FLATTENERS.add(flattener);
        }
    }

    public static void unregister(Flattener flattener) {
        FLATTENERS.remove(flattener);
    }

    /**
     * Turn off new signals, then ask every attached MAT instance to close
     * its position with a market order (no brackets).
     */
    public static void flattenAll() {
        MatSideFilter.set(false, false, "flatten");
        if (FLATTENERS.isEmpty()) {
            Log.info("MAT FLATTEN: no MAT instance registered (side filter set to NONE)");
            return;
        }
        Log.info("MAT FLATTEN: requesting " + FLATTENERS.size() + " instance(s)");
        for (Flattener f : FLATTENERS) {
            try {
                f.flatten();
            } catch (Throwable t) {
                Log.info("MAT FLATTEN instance failed: " + t);
            }
        }
    }
}
