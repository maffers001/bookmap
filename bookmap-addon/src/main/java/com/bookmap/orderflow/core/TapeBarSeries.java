package com.bookmap.orderflow.core;

import java.util.Map;
import java.util.TreeMap;

/**
 * Per-instrument fixed-width tape bars keyed by wall-clock slot start, matching
 * {@code research/features/momentum.py::trades_to_tape_bars} (slot =
 * {@code ts / barNs * barNs}; a slot only exists if it had prints).
 *
 * Unlike {@link TapeBurstTracker} this keeps <em>every</em> closed bar for a
 * short lookback so two instruments can be compared on the same slot
 * (correlated exhaustion / fade). A slot is complete once the exchange clock
 * reaches {@code slot + barNs}; callers must only read bars with
 * {@link #isComplete(long, long)} true to stay causal.
 */
public final class TapeBarSeries {

    public static final class Bar {
        public final long slotNs;
        public double buyVol;
        public double sellVol;
        public int nTrades;
        public double open;
        public double high;
        public double low;
        public double close;

        Bar(long slotNs, double price, int size, boolean buyAggressor) {
            this.slotNs = slotNs;
            this.open = this.high = this.low = this.close = price;
            add(price, size, buyAggressor);
        }

        void add(double price, int size, boolean buyAggressor) {
            if (buyAggressor) {
                buyVol += size;
            } else {
                sellVol += size;
            }
            nTrades++;
            high = Math.max(high, price);
            low = Math.min(low, price);
            close = price;
        }

        public double totalVol() {
            return buyVol + sellVol;
        }

        /** (buy − sell) / total; 0 when empty. */
        public double imbalance() {
            double t = totalVol();
            return t > 0 ? (buyVol - sellVol) / t : 0.0;
        }

        public double net() {
            return close - open;
        }
    }

    private final long barNs;
    private final int maxBars;
    private final TreeMap<Long, Bar> bars = new TreeMap<>();

    public TapeBarSeries(int barMs, int maxBars) {
        this.barNs = Math.max(1, barMs) * 1_000_000L;
        this.maxBars = Math.max(10, maxBars);
    }

    public long barNs() {
        return barNs;
    }

    public long slotOf(long tsNs) {
        return (tsNs / barNs) * barNs;
    }

    public boolean isComplete(long slotNs, long nowNs) {
        return nowNs >= slotNs + barNs;
    }

    public void onTrade(long tsNs, double price, int size, boolean buyAggressor) {
        long slot = slotOf(tsNs);
        Bar b = bars.get(slot);
        if (b == null) {
            bars.put(slot, new Bar(slot, price, size, buyAggressor));
            while (bars.size() > maxBars) {
                bars.pollFirstEntry();
            }
        } else {
            b.add(price, size, buyAggressor);
        }
    }

    /** Bar for an exact slot start, or null if that slot had no prints (yet). */
    public Bar get(long slotNs) {
        return bars.get(slotNs);
    }

    public Map.Entry<Long, Bar> last() {
        return bars.lastEntry();
    }

    public int size() {
        return bars.size();
    }

    public void reset() {
        bars.clear();
    }
}
