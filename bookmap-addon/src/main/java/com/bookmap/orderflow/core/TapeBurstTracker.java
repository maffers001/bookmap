package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;

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
    /** Recent completed bursts (research pairs any within sync, not only last). */
    private final Deque<Burst> recent = new ArrayDeque<>();
    private static final int MAX_RECENT = 64;
    private static final long RECENT_KEEP_NS = 5_000_000_000L; // 5s

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
        return findRecent(side, nowNs, windowNs) != null;
    }

    public long lagMsTo(int side, long nowNs) {
        Burst b = findRecent(side, nowNs, Long.MAX_VALUE / 4);
        if (b == null) {
            return Long.MAX_VALUE;
        }
        long dt = nowNs - b.tsNs;
        if (dt < 0) {
            return Long.MAX_VALUE;
        }
        return dt / 1_000_000L;
    }

    /**
     * Research builds all bars offline then pairs. Live often closes NQ before
     * the same 100ms ES bar (no ES print in the next slot yet). If the open ES
     * bar is that same slot and would qualify, treat it as a completed burst.
     */
    public Burst peekOpenBarAsBurst(long nqBarEndTsNs) {
        if (barStartNs < 0 || nqBarEndTsNs <= 0) {
            return null;
        }
        long slot = (nqBarEndTsNs / barNs) * barNs;
        if (barStartNs != slot) {
            return null;
        }
        return qualifyCurrent(barStartNs + barNs - 1, /*record*/ false);
    }

    private Burst findRecent(int side, long nowNs, long windowNs) {
        trimRecent(nowNs);
        Burst best = null;
        long bestDt = Long.MAX_VALUE;
        for (Burst b : recent) {
            if (b.side != side) {
                continue;
            }
            long dt = nowNs - b.tsNs;
            if (dt < 0 || dt > windowNs) {
                continue;
            }
            if (dt < bestDt) {
                bestDt = dt;
                best = b;
            }
        }
        // Also lastBurst in case recent was cleared
        if (lastBurst != null && lastBurst.side == side) {
            long dt = nowNs - lastBurst.tsNs;
            if (dt >= 0 && dt <= windowNs && dt < bestDt) {
                best = lastBurst;
            }
        }
        return best;
    }

    private void trimRecent(long nowNs) {
        while (!recent.isEmpty() && nowNs - recent.peekFirst().tsNs > RECENT_KEEP_NS) {
            recent.removeFirst();
        }
        while (recent.size() > MAX_RECENT) {
            recent.removeFirst();
        }
    }

    /** Clear bar/burst state (call when replay clock jumps backwards). */
    public void reset() {
        barStartNs = -1;
        buyVol = sellVol = 0;
        nTrades = 0;
        openPx = highPx = lowPx = closePx = Double.NaN;
        barsSinceBurst = 1_000_000;
        lastBurst = null;
        recent.clear();
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
        Burst b = qualifyCurrent(endTsNs, true);
        return b;
    }

    private Burst qualifyCurrent(long endTsNs, boolean record) {
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
        if (!(longBurst || shortBurst)) {
            return null;
        }
        // finishBar increments barsSinceBurst before calling; peek simulates that +1
        int since = record ? barsSinceBurst : barsSinceBurst + 1;
        if (since < cooldownBars) {
            return null;
        }
        Burst b = new Burst(
                endTsNs,
                longBurst ? 1 : -1,
                nTrades,
                total,
                imbalance,
                net,
                closePx);
        if (record) {
            barsSinceBurst = 0;
            lastBurst = b;
            recent.addLast(b);
            trimRecent(endTsNs);
        }
        return b;
    }
}
