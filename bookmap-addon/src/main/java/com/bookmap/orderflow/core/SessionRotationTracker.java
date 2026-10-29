package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/**
 * Causal session-scale rotation state (research H59 / EXP-0068 / EXP-0075).
 *
 * <p>Swings: lookback extreme not exceeded for {@code confirmMs} (defaults
 * 15 min / 60 s). Completed rotations shorter than {@code minRotationPts} are
 * dropped. Open-leg ratio = extension / mean of last {@code avgN} same-direction
 * completed rotations (need ≥ {@code minSameDir}). When not ready, callers treat
 * the trade as fresh (keep) — matches research {@code rot_ready==0}.
 */
public final class SessionRotationTracker {

    public static final class Snapshot {
        public final double rotRatio;
        public final int rotLegDir;
        public final boolean rotReady;
        public final double extensionPts;
        public final double avgPts;
        public final boolean fromPrior;
        public final int nSameDir;
        public final int nPivots;
        public final int nRots;

        Snapshot(double rotRatio, int rotLegDir, boolean rotReady,
                 double extensionPts, double avgPts, boolean fromPrior,
                 int nSameDir, int nPivots, int nRots) {
            this.rotRatio = rotRatio;
            this.rotLegDir = rotLegDir;
            this.rotReady = rotReady;
            this.extensionPts = extensionPts;
            this.avgPts = avgPts;
            this.fromPrior = fromPrior;
            this.nSameDir = nSameDir;
            this.nPivots = nPivots;
            this.nRots = nRots;
        }

        public static Snapshot empty() {
            return new Snapshot(Double.NaN, 0, false, Double.NaN, Double.NaN, false, 0, 0, 0);
        }
    }

    private static final class Trade {
        final long tsNs;
        final long pxTicks;

        Trade(long tsNs, long pxTicks) {
            this.tsNs = tsNs;
            this.pxTicks = pxTicks;
        }
    }

    private static final class Pivot {
        final long tsNs;
        final long pxTicks;
        final boolean high; // true=HIGH, false=LOW
        final long confirmTsNs;

        Pivot(long tsNs, long pxTicks, boolean high, long confirmTsNs) {
            this.tsNs = tsNs;
            this.pxTicks = pxTicks;
            this.high = high;
            this.confirmTsNs = confirmTsNs;
        }
    }

    private static final class Rotation {
        final long endConfirmTsNs;
        final double lengthPts;
        final int direction; // +1 up, -1 down

        Rotation(long endConfirmTsNs, double lengthPts, int direction) {
            this.endConfirmTsNs = endConfirmTsNs;
            this.lengthPts = lengthPts;
            this.direction = direction;
        }
    }

    private final long lookbackNs;
    private final long confirmNs;
    private final double minRotationPts;
    private final double ptsPerTick;
    private final int avgN;
    private final int minSameDir;

    private final Deque<Trade> trades = new ArrayDeque<>();
    private final List<Pivot> pivots = new ArrayList<>();
    private final List<Rotation> rotations = new ArrayList<>();

    private Trade pendingHigh; // extreme ts/px; confirm clock from extreme ts
    private Trade pendingLow;
    private long lastPxTicks = Long.MIN_VALUE;

    /** Prior-session same-dir means (research fallback when session not ready). */
    private double priorUpAvgPts = Double.NaN;
    private double priorDownAvgPts = Double.NaN;

    public SessionRotationTracker() {
        this(900_000L, 60_000L, 15.0, 0.25, 4, 2);
    }

    public SessionRotationTracker(
            long lookbackMs,
            long confirmMs,
            double minRotationPts,
            double ptsPerTick,
            int avgN,
            int minSameDir) {
        this.lookbackNs = Math.max(1L, lookbackMs) * 1_000_000L;
        this.confirmNs = Math.max(1L, confirmMs) * 1_000_000L;
        this.minRotationPts = minRotationPts;
        this.ptsPerTick = ptsPerTick > 0 ? ptsPerTick : 0.25;
        this.avgN = Math.max(1, avgN);
        this.minSameDir = Math.max(1, minSameDir);
    }

    public void reset() {
        trades.clear();
        pivots.clear();
        rotations.clear();
        pendingHigh = null;
        pendingLow = null;
        lastPxTicks = Long.MIN_VALUE;
        // keep prior* across reset (session-open restart)
    }

    /** Causal prior-session averages (pts). NaN = unused. */
    public void setPriorAvgs(double upAvgPts, double downAvgPts) {
        this.priorUpAvgPts = upAvgPts;
        this.priorDownAvgPts = downAvgPts;
    }

    public double priorUpAvgPts() {
        return priorUpAvgPts;
    }

    public double priorDownAvgPts() {
        return priorDownAvgPts;
    }

    /** Mean length of completed same-dir rotations this session (for persist). */
    public double sessionAvgPts(int direction) {
        List<Double> lengths = new ArrayList<>();
        for (Rotation r : rotations) {
            if (r.direction != direction) {
                continue;
            }
            lengths.add(r.lengthPts);
        }
        if (lengths.isEmpty()) {
            return Double.NaN;
        }
        int from = Math.max(0, lengths.size() - avgN);
        double sum = 0;
        int n = 0;
        for (int i = from; i < lengths.size(); i++) {
            sum += lengths.get(i);
            n++;
        }
        return n > 0 ? sum / n : Double.NaN;
    }

    public int rotationCount() {
        return rotations.size();
    }

    /**
     * @param tsNs exchange/replay time ns
     * @param apiPrice Bookmap API trade price (tick index)
     */
    public void onTrade(long tsNs, double apiPrice) {
        long pxTicks = Math.round(apiPrice);
        lastPxTicks = pxTicks;
        trades.addLast(new Trade(tsNs, pxTicks));
        trimTrades(tsNs);

        if (pendingHigh != null) {
            if (pxTicks > pendingHigh.pxTicks) {
                pendingHigh = null;
            } else if (tsNs - pendingHigh.tsNs >= confirmNs) {
                addPivot(new Pivot(pendingHigh.tsNs, pendingHigh.pxTicks, true, tsNs));
                pendingHigh = null;
            }
        }
        if (pendingLow != null) {
            if (pxTicks < pendingLow.pxTicks) {
                pendingLow = null;
            } else if (tsNs - pendingLow.tsNs >= confirmNs) {
                addPivot(new Pivot(pendingLow.tsNs, pendingLow.pxTicks, false, tsNs));
                pendingLow = null;
            }
        }

        long loT = tsNs - lookbackNs;
        // window = trades strictly before current (exclude last just added)
        if (trades.size() < 4) {
            return;
        }
        long hiPx = Long.MIN_VALUE;
        long loPx = Long.MAX_VALUE;
        long hiTs = 0;
        long loTs = 0;
        long lastWinPx = 0;
        long lastWinTs = 0;
        int winCount = 0;
        for (Trade t : trades) {
            if (t.tsNs >= tsNs) {
                break;
            }
            if (t.tsNs < loT) {
                continue;
            }
            winCount++;
            lastWinPx = t.pxTicks;
            lastWinTs = t.tsNs;
            if (t.pxTicks >= hiPx) {
                hiPx = t.pxTicks;
                hiTs = t.tsNs;
            }
            if (t.pxTicks <= loPx) {
                loPx = t.pxTicks;
                loTs = t.tsNs;
            }
        }
        if (winCount < 3) {
            return;
        }
        // Candidate high: last trade of lookback equals the max, current does not exceed
        if (hiPx == lastWinPx && pxTicks <= lastWinPx) {
            if (pendingHigh == null || pendingHigh.pxTicks != lastWinPx) {
                pendingHigh = new Trade(lastWinTs, lastWinPx);
            }
        }
        if (loPx == lastWinPx && pxTicks >= lastWinPx) {
            if (pendingLow == null || pendingLow.pxTicks != lastWinPx) {
                pendingLow = new Trade(lastWinTs, lastWinPx);
            }
        }
    }

    public Snapshot snapshot(long asofNs) {
        if (lastPxTicks == Long.MIN_VALUE) {
            return Snapshot.empty();
        }
        OpenLeg leg = openLeg(asofNs, lastPxTicks);
        if (leg == null) {
            return new Snapshot(Double.NaN, 0, false, Double.NaN, Double.NaN, false,
                    0, pivots.size(), rotations.size());
        }
        AvgResult ar = avgSameDir(asofNs, leg.dir);
        boolean ready = ar.avg != null && ar.avg > 0;
        double ratio = ready ? leg.extensionPts / ar.avg : Double.NaN;
        return new Snapshot(ratio, leg.dir, ready, leg.extensionPts,
                ready ? ar.avg : Double.NaN, ar.fromPrior, ar.nSameDir,
                pivots.size(), rotations.size());
    }

    /**
     * Fresh keep-gate: not ready, or with-leg and ratio &lt; maxRatio.
     * {@code tradeSide} is +1 long / −1 short.
     */
    public boolean isRotFresh(long asofNs, int tradeSide, double maxRatio) {
        Snapshot s = snapshot(asofNs);
        if (!s.rotReady) {
            return true;
        }
        boolean withLeg = tradeSide != 0 && tradeSide == s.rotLegDir;
        return withLeg && Double.isFinite(s.rotRatio) && s.rotRatio < maxRatio;
    }

    private void trimTrades(long nowNs) {
        long keepFrom = nowNs - lookbackNs - confirmNs - 60_000_000_000L; // +60s slack
        while (!trades.isEmpty() && trades.peekFirst().tsNs < keepFrom) {
            trades.removeFirst();
        }
    }

    private void addPivot(Pivot p) {
        pivots.add(p);
        // Research rebuilds the alternating chain from all pivots each time.
        // Incremental append left stale rotations after same-kind compaction —
        // that inflated same-dir averages / false rot_fresh fails.
        rebuildRotationsFromPivots();
    }

    /**
     * Match {@code completed_session_rotations}: compact same-kind along the
     * full chain, then emit one rotation per alternating adjacent pair ≥ min pts.
     */
    private void rebuildRotationsFromPivots() {
        if (pivots.isEmpty()) {
            rotations.clear();
            return;
        }
        // Sort by confirm then extreme ts (stable causal order)
        List<Pivot> ordered = new ArrayList<>(pivots);
        ordered.sort((a, b) -> {
            int c = Long.compare(a.confirmTsNs, b.confirmTsNs);
            return c != 0 ? c : Long.compare(a.tsNs, b.tsNs);
        });
        List<Pivot> chain = new ArrayList<>();
        for (Pivot p : ordered) {
            if (chain.isEmpty()) {
                chain.add(p);
                continue;
            }
            Pivot last = chain.get(chain.size() - 1);
            if (p.high == last.high) {
                boolean takeP = p.high
                        ? p.pxTicks >= last.pxTicks
                        : p.pxTicks <= last.pxTicks;
                if (takeP) {
                    chain.set(chain.size() - 1, p);
                }
                continue;
            }
            chain.add(p);
        }
        pivots.clear();
        pivots.addAll(chain);
        rotations.clear();
        for (int i = 1; i < chain.size(); i++) {
            Pivot a = chain.get(i - 1);
            Pivot b = chain.get(i);
            double length = Math.abs(b.pxTicks - a.pxTicks) * ptsPerTick;
            if (length < minRotationPts) {
                continue;
            }
            int dir = b.high ? 1 : -1;
            rotations.add(new Rotation(b.confirmTsNs, length, dir));
        }
    }

    private static final class OpenLeg {
        final int dir;
        final double extensionPts;

        OpenLeg(int dir, double extensionPts) {
            this.dir = dir;
            this.extensionPts = extensionPts;
        }
    }

    private OpenLeg openLeg(long asofNs, long pxTicks) {
        Pivot low = lastPivot(asofNs, false);
        Pivot high = lastPivot(asofNs, true);
        Pivot last = lastPivot(asofNs, null);
        if (last == null) {
            return null;
        }
        if (low != null && high != null) {
            if (pxTicks >= high.pxTicks) {
                return new OpenLeg(1, (pxTicks - low.pxTicks) * ptsPerTick);
            }
            if (pxTicks <= low.pxTicks) {
                return new OpenLeg(-1, (high.pxTicks - pxTicks) * ptsPerTick);
            }
        }
        if (!last.high) {
            return new OpenLeg(1, Math.max(0, pxTicks - last.pxTicks) * ptsPerTick);
        }
        return new OpenLeg(-1, Math.max(0, last.pxTicks - pxTicks) * ptsPerTick);
    }

    private Pivot lastPivot(long asofNs, Boolean highOrNull) {
        Pivot best = null;
        for (Pivot p : pivots) {
            if (p.confirmTsNs > asofNs) {
                continue;
            }
            if (highOrNull != null && p.high != highOrNull) {
                continue;
            }
            if (best == null || p.confirmTsNs > best.confirmTsNs) {
                best = p;
            }
        }
        return best;
    }

    private static final class AvgResult {
        final Double avg;
        final boolean fromPrior;
        final int nSameDir;

        AvgResult(Double avg, boolean fromPrior, int nSameDir) {
            this.avg = avg;
            this.fromPrior = fromPrior;
            this.nSameDir = nSameDir;
        }
    }

    private AvgResult avgSameDir(long asofNs, int direction) {
        List<Double> lengths = new ArrayList<>();
        for (Rotation r : rotations) {
            if (r.endConfirmTsNs > asofNs) {
                break;
            }
            if (r.direction != direction) {
                continue;
            }
            lengths.add(r.lengthPts);
        }
        if (lengths.size() >= minSameDir) {
            int from = Math.max(0, lengths.size() - avgN);
            double sum = 0;
            int n = 0;
            for (int i = from; i < lengths.size(); i++) {
                sum += lengths.get(i);
                n++;
            }
            return new AvgResult(n > 0 ? sum / n : null, false, lengths.size());
        }
        // Research fallback: prior clean session same-direction mean
        double prior = direction > 0 ? priorUpAvgPts : priorDownAvgPts;
        if (Double.isFinite(prior) && prior > 0) {
            return new AvgResult(prior, true, lengths.size());
        }
        return new AvgResult(null, false, lengths.size());
    }
}
