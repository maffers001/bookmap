package com.bookmap.orderflow.core;

/**
 * 100ms (configurable) tape-burst detector matching research EXP-0008:
 * one-sided aggression + fast prints + price progress.
 */
public final class TapeBurstTracker {

    public static final class Burst {
        public final long tsNs;
        public final int side; // +1 buy/long, -1 sell/short
        public final int nTrades;
        public final double totalVol;
        public final double imbalance;
        public final double netPrice;
        public final double closePrice;

        Burst(long tsNs, int side, int nTrades, double totalVol, double imbalance,
              double netPrice, double closePrice) {
            this.tsNs = tsNs;
            this.side = side;
            this.nTrades = nTrades;
            this.totalVol = totalVol;
            this.imbalance = imbalance;
            this.netPrice = netPrice;
            this.closePrice = closePrice;
        }
    }

    private final long barNs;
    private final int minTrades;
    private final double minVol;
    private final double minImbalance;
    private final double minProgressPrice;
    private final int cooldownBars;

    private long barStartNs = -1;
    private double buyVol;
    private double sellVol;
    private int nTrades;
    private double openPx = Double.NaN;
    private double highPx = Double.NaN;
    private double lowPx = Double.NaN;
    private double closePx = Double.NaN;

    private int barsSinceBurst = 1_000_000;
    private Burst lastBurst;

    public TapeBurstTracker(int barMs, int minTrades, double minVol, double minImbalance,
                            double minProgressPrice, int cooldownBars) {
        this.barNs = Math.max(1, barMs) * 1_000_000L;
        this.minTrades = minTrades;
        this.minVol = minVol;
        this.minImbalance = minImbalance;
        this.minProgressPrice = minProgressPrice;
        this.cooldownBars = Math.max(1, cooldownBars);
    }

    /** Feed a trade; returns a Burst if the just-closed bar qualifies. */
    public Burst onTrade(long tsNs, double price, int size, boolean buyAggressor) {
        Burst closed = null;
        if (barStartNs < 0) {
            startBar(tsNs, price, size, buyAggressor);
            return null;
        }
        long slot = (tsNs / barNs) * barNs;
        if (slot != barStartNs) {
            closed = finishBar(barStartNs + barNs - 1);
            startBar(slot, price, size, buyAggressor);
        } else {
            accumulate(price, size, buyAggressor);
        }
        return closed;
    }

    public Burst getLastBurst() {
        return lastBurst;
    }

    public boolean hasRecentBurst(int side, long nowNs, long windowNs) {
        if (lastBurst == null || lastBurst.side != side) {
            return false;
        }
        long dt = nowNs - lastBurst.tsNs;
        // Replay rewind: burst in the "future" relative to current clock is invalid
        if (dt < 0) {
            return false;
        }
        return dt <= windowNs;
    }

    public long lagMsTo(int side, long nowNs) {
        if (lastBurst == null || lastBurst.side != side) {
            return Long.MAX_VALUE;
        }
        long dt = nowNs - lastBurst.tsNs;
        if (dt < 0) {
            return Long.MAX_VALUE; // stale after rewind
        }
        return dt / 1_000_000L;
    }

    /** Clear bar/burst state (call when replay clock jumps backwards). */
    public void reset() {
        barStartNs = -1;
        buyVol = sellVol = 0;
        nTrades = 0;
        openPx = highPx = lowPx = closePx = Double.NaN;
        barsSinceBurst = 1_000_000;
        lastBurst = null;
    }

    private void startBar(long slot, double price, int size, boolean buyAggressor) {
        barStartNs = slot;
        buyVol = sellVol = 0;
        nTrades = 0;
        openPx = highPx = lowPx = closePx = price;
        accumulate(price, size, buyAggressor);
    }

    private void accumulate(double price, int size, boolean buyAggressor) {
        if (buyAggressor) {
            buyVol += size;
        } else {
            sellVol += size;
        }
        nTrades++;
        highPx = Math.max(highPx, price);
        lowPx = Math.min(lowPx, price);
        closePx = price;
    }

    private Burst finishBar(long endTsNs) {
        barsSinceBurst++;
        double total = buyVol + sellVol;
        if (total <= 0 || nTrades <= 0 || Double.isNaN(openPx)) {
            return null;
        }
        double imbalance = (buyVol - sellVol) / total;
        double net = closePx - openPx;
        boolean fast = nTrades >= minTrades && total >= minVol;
        boolean longBurst = fast && imbalance >= minImbalance && net > 0
                && Math.abs(net) >= minProgressPrice;
        boolean shortBurst = fast && imbalance <= -minImbalance && net < 0
                && Math.abs(net) >= minProgressPrice;
        if (!(longBurst || shortBurst) || barsSinceBurst < cooldownBars) {
            return null;
        }
        barsSinceBurst = 0;
        lastBurst = new Burst(
                endTsNs,
                longBurst ? 1 : -1,
                nTrades,
                total,
                imbalance,
                net,
                closePx);
        return lastBurst;
    }
}
