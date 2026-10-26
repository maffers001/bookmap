package com.bookmap.orderflow.core;

import java.util.Map;
import java.util.TreeMap;

/**
 * Developing session volume profile from a session-open stamp.
 *
 * <p>Accumulates trade volume by price tick and computes VPOC plus a contiguous
 * value area (default 70%) by expanding from VPOC — same rules as
 * {@code research.features.range_bar_profile.value_area_from_profile}.
 */
public final class MarketProfileTracker {

    /** Snapshot of developing levels in chart points (tick × pips). */
    public static final class Levels {
        public final double vpocPts;
        public final double vahPts;
        public final double valPts;
        public final long totalVol;
        public final long vpocVol;
        public final double vaFracCovered;

        public Levels(
                double vpocPts,
                double vahPts,
                double valPts,
                long totalVol,
                long vpocVol,
                double vaFracCovered) {
            this.vpocPts = vpocPts;
            this.vahPts = vahPts;
            this.valPts = valPts;
            this.totalVol = totalVol;
            this.vpocVol = vpocVol;
            this.vaFracCovered = vaFracCovered;
        }

        public boolean ready() {
            return totalVol > 0
                    && Double.isFinite(vpocPts)
                    && Double.isFinite(vahPts)
                    && Double.isFinite(valPts);
        }
    }

    private final double pips;
    private double vaFrac;

    /** Volume at each price tick since session open. */
    private final TreeMap<Integer, Long> volByTick = new TreeMap<>();
    private long totalVol;
    private long volumeSumTicks; // Σ(tick × vol) for VWAP tie-break
    private boolean open;
    private long openTsNs;
    private int ukDayOfYear = Integer.MIN_VALUE;

    private Levels cached = emptyLevels();
    private boolean dirty = true;

    public MarketProfileTracker(double pips, double vaFrac) {
        this.pips = pips > 0 ? pips : 0.25;
        setVaFrac(vaFrac);
    }

    public void setVaFrac(double vaFrac) {
        double f = vaFrac;
        if (!Double.isFinite(f) || f <= 0 || f > 1.0) {
            f = 0.70;
        }
        if (Math.abs(this.vaFrac - f) > 1e-12) {
            this.vaFrac = f;
            dirty = true;
        }
    }

    public double vaFrac() {
        return vaFrac;
    }

    public boolean isOpen() {
        return open;
    }

    public long openTsNs() {
        return openTsNs;
    }

    public long totalVol() {
        return totalVol;
    }

    /** Clear profile and mark not collecting (e.g. UK day rollover before open). */
    public void reset() {
        volByTick.clear();
        totalVol = 0;
        volumeSumTicks = 0;
        open = false;
        openTsNs = 0;
        dirty = true;
        cached = emptyLevels();
    }

    /**
     * Begin (or restart) the session profile at {@code tsNs}.
     * Clears any prior accumulation for the new session.
     */
    public void markSessionOpen(long tsNs) {
        volByTick.clear();
        totalVol = 0;
        volumeSumTicks = 0;
        open = true;
        openTsNs = tsNs;
        dirty = true;
        cached = emptyLevels();
    }

    /** Remember the UK day-of-year last seen (for day-change detection in the module). */
    public void setUkDayOfYear(int dayOfYear) {
        this.ukDayOfYear = dayOfYear;
    }

    public int ukDayOfYear() {
        return ukDayOfYear;
    }

    public void onTrade(int priceTick, int size) {
        if (!open || size <= 0) {
            return;
        }
        volByTick.merge(priceTick, (long) size, Long::sum);
        totalVol += size;
        volumeSumTicks += (long) priceTick * size;
        dirty = true;
    }

    /** Developing VAL / VAH / VPOC in chart points. Recomputes only when dirty. */
    public Levels levels() {
        if (!dirty) {
            return cached;
        }
        dirty = false;
        cached = compute();
        return cached;
    }

    private Levels compute() {
        if (!open || totalVol <= 0 || volByTick.isEmpty()) {
            return emptyLevels();
        }

        int n = volByTick.size();
        int[] ticks = new int[n];
        long[] vols = new long[n];
        int i = 0;
        for (Map.Entry<Integer, Long> e : volByTick.entrySet()) {
            ticks[i] = e.getKey();
            vols[i] = e.getValue();
            i++;
        }

        double vwapTick = (double) volumeSumTicks / (double) totalVol;

        // VPOC = max volume; tie-break closest to VWAP, then lower tick
        long maxV = 0;
        for (long v : vols) {
            if (v > maxV) {
                maxV = v;
            }
        }
        int pocI = -1;
        double bestDist = Double.POSITIVE_INFINITY;
        for (int j = 0; j < n; j++) {
            if (vols[j] < maxV) {
                continue;
            }
            double dist = Math.abs(ticks[j] - vwapTick);
            if (pocI < 0
                    || dist < bestDist - 1e-12
                    || (Math.abs(dist - bestDist) <= 1e-12 && ticks[j] < ticks[pocI])) {
                pocI = j;
                bestDist = dist;
            }
        }
        if (pocI < 0) {
            return emptyLevels();
        }

        int lo = pocI;
        int hi = pocI;
        long covered = vols[pocI];
        double target = vaFrac * totalVol;
        while (covered + 1e-9 < target && (lo > 0 || hi < n - 1)) {
            long left = lo > 0 ? vols[lo - 1] : -1L;
            long right = hi < n - 1 ? vols[hi + 1] : -1L;
            if (right > left) {
                hi++;
                covered += vols[hi];
            } else if (left > right) {
                lo--;
                covered += vols[lo];
            } else if (lo > 0) {
                // Tie: expand lower side first (matches research)
                lo--;
                covered += vols[lo];
            } else if (hi < n - 1) {
                hi++;
                covered += vols[hi];
            } else {
                break;
            }
        }

        double ppt = pips;
        return new Levels(
                ticks[pocI] * ppt,
                ticks[hi] * ppt,
                ticks[lo] * ppt,
                totalVol,
                vols[pocI],
                totalVol > 0 ? (double) covered / (double) totalVol : 0.0);
    }

    private static Levels emptyLevels() {
        return new Levels(
                Double.NaN, Double.NaN, Double.NaN, 0L, 0L, 0.0);
    }
}
