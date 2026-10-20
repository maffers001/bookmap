package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.TreeMap;

/**
 * High-volume local peak/trough, then mark when price moves N ticks away on a
 * <b>clean</b> leg (optional efficiency / max pullback / max time / quiet-leg gates).
 *
 * <ol>
 *   <li>Track volume by price tick over a rolling lookback.</li>
 *   <li>When price reverses {@code confirmTicks} from a running high/low, that
 *       extreme is a candidate peak/trough.</li>
 *   <li>Keep it if volume in a small band around the extreme ≥
 *       {@code highVolMult × avgVolPerTick × bandWidth}.</li>
 *   <li>While watching, accumulate path length, max giveback, and volume on
 *       prints <b>outside</b> the peak band (leg away from the tip).</li>
 *   <li>When price is {@code moveTicks} away, fire only if the leg passes clean
 *       gates. Cancel on a new extreme or failed gate.</li>
 * </ol>
 *
 * Quiet-leg gates (either {@code 0} = off):
 * <ul>
 *   <li>Gate B: {@code legVol / (avgVolPerTick × outsideTicks) < maxLegVolVsAvg}
 *       where {@code outsideTicks = max(1, moveTicks − bandHalf)}.</li>
 *   <li>Gate A: {@code legVol / extremeVol < maxLegVolVsTip}.</li>
 * </ul>
 * Efficiency / pullback / time / leg-vol gates: {@code 0} disables that gate.
 * All distances are in <b>ticks</b> (API price units on this install).
 */
public final class HvExtremeMoveTracker {

    public enum Side {
        NONE,
        LONG,
        SHORT
    }

    public static final class Signal {
        public final Side side;
        public final int extremeTick;
        public final int triggerTick;
        public final double extremeVol;
        public final double avgVolPerTick;
        public final double volRatio;
        public final int moveTicks;
        public final double pathEfficiency;
        public final int maxPullbackTicks;
        public final double legSec;
        /** Volume on prints outside the peak band during the arm→fire leg. */
        public final double legVol;
        /** {@code legVol / (avgVolPerTick × outsideTicks)}. */
        public final double legVolRatio;
        /** {@code legVol / extremeVol}. */
        public final double legVolVsTip;

        public Signal(
                Side side,
                int extremeTick,
                int triggerTick,
                double extremeVol,
                double avgVolPerTick,
                double volRatio,
                int moveTicks,
                double pathEfficiency,
                int maxPullbackTicks,
                double legSec,
                double legVol,
                double legVolRatio,
                double legVolVsTip) {
            this.side = side;
            this.extremeTick = extremeTick;
            this.triggerTick = triggerTick;
            this.extremeVol = extremeVol;
            this.avgVolPerTick = avgVolPerTick;
            this.volRatio = volRatio;
            this.moveTicks = moveTicks;
            this.pathEfficiency = pathEfficiency;
            this.maxPullbackTicks = maxPullbackTicks;
            this.legSec = legSec;
            this.legVol = legVol;
            this.legVolRatio = legVolRatio;
            this.legVolVsTip = legVolVsTip;
        }
    }

    private final int moveTicks;
    private final int confirmTicks;
    private final int extremeBandTicks;
    private final double highVolMult;
    private final long lookbackNs;
    private final long cooldownNs;
    /** Displacement / path length; 0 = off. */
    private final double minPathEfficiency;
    /** Max giveback toward tip during the leg; 0 = off. */
    private final int maxPullbackTicksAllowed;
    /** Max time from arm to fire; 0 = off. */
    private final long maxLegNs;
    /**
     * Max allowed {@code legVol / (avgVolPerTick × outsideTicks)}; 0 = off.
     * Fire only when the ratio is strictly below this threshold.
     */
    private final double maxLegVolVsAvg;
    /**
     * Max allowed {@code legVol / extremeVol}; 0 = off.
     * Fire only when the ratio is strictly below this threshold.
     */
    private final double maxLegVolVsTip;

    private final Deque<Print> prints = new ArrayDeque<>();
    private final TreeMap<Integer, Double> volByTick = new TreeMap<>();
    private double totalVol;

    private int lastTick = Integer.MIN_VALUE;
    private int runHi = Integer.MIN_VALUE / 4;
    private int runLo = Integer.MAX_VALUE / 4;
    private long runHiNs;
    private long runLoNs;

    private Side watching = Side.NONE;
    private int extremeTick;
    private double extremeVol;
    private double extremeVolRatio;
    private long armNs;
    private int pathLastTick;
    private long pathLengthTicks;
    private int maxPullbackTicks;
    /** Favorable watermark since arm (high for LONG, low for SHORT). */
    private int favorTick;
    /** Volume outside peak band while armed. */
    private double legVol;

    private long lastSignalNs = Long.MIN_VALUE / 4;
    private Signal pending;

    private static final class Print {
        final long ns;
        final int tick;
        final double size;

        Print(long ns, int tick, double size) {
            this.ns = ns;
            this.tick = tick;
            this.size = size;
        }
    }

    public HvExtremeMoveTracker(
            int moveTicks,
            int confirmTicks,
            int extremeBandTicks,
            double highVolMult,
            long lookbackNs,
            long cooldownNs,
            double minPathEfficiency,
            int maxPullbackTicksAllowed,
            long maxLegNs,
            double maxLegVolVsAvg,
            double maxLegVolVsTip) {
        this.moveTicks = Math.max(1, moveTicks);
        this.confirmTicks = Math.max(1, confirmTicks);
        this.extremeBandTicks = Math.max(0, extremeBandTicks);
        this.highVolMult = Math.max(0.1, highVolMult);
        this.lookbackNs = Math.max(1_000_000_000L, lookbackNs);
        this.cooldownNs = Math.max(0L, cooldownNs);
        this.minPathEfficiency = Math.max(0.0, minPathEfficiency);
        this.maxPullbackTicksAllowed = Math.max(0, maxPullbackTicksAllowed);
        this.maxLegNs = Math.max(0L, maxLegNs);
        this.maxLegVolVsAvg = Math.max(0.0, maxLegVolVsAvg);
        this.maxLegVolVsTip = Math.max(0.0, maxLegVolVsTip);
    }

    /** Ticks of the move that lie outside the peak half-band. */
    private int outsideTicks() {
        return Math.max(1, moveTicks - extremeBandTicks);
    }

    public void onTrade(long tNs, int tick, int size) {
        if (size <= 0) {
            return;
        }
        int prev = lastTick;
        lastTick = tick;
        add(tNs, tick, size);
        prune(tNs);

        if (tick >= runHi) {
            runHi = tick;
            runHiNs = tNs;
        }
        if (tick <= runLo) {
            runLo = tick;
            runLoNs = tNs;
        }

        if (watching == Side.NONE) {
            tryWatch(tNs, tick, size);
        } else {
            if (isOutsidePeakBand(tick)) {
                legVol += size;
            }
            updateWatch(tNs, tick, prev, size);
        }
    }

    public Signal pollSignal() {
        Signal s = pending;
        pending = null;
        return s;
    }

    public Side watchingSide() {
        return watching;
    }

    public int extremeTick() {
        return extremeTick;
    }

    public int moveTicks() {
        return moveTicks;
    }

    public double currentExtremeVolRatio() {
        return extremeVolRatio;
    }

    /** Live path efficiency while armed; 0 when idle / unknown. */
    public double currentPathEfficiency() {
        if (watching == Side.NONE || pathLengthTicks <= 0 || lastTick == Integer.MIN_VALUE) {
            return 0;
        }
        int disp = Math.abs(lastTick - extremeTick);
        return disp / (double) pathLengthTicks;
    }

    /** Live max pullback (ticks) while armed. */
    public int currentMaxPullbackTicks() {
        return watching == Side.NONE ? 0 : maxPullbackTicks;
    }

    /** Live Gate-B ratio while armed; 0 when idle. */
    public double currentLegVolRatio() {
        if (watching == Side.NONE) {
            return 0;
        }
        double avg = avgVolPerTick();
        if (avg <= 0) {
            return 0;
        }
        return legVol / (avg * outsideTicks());
    }

    private void tryWatch(long tNs, int tick, int size) {
        if (tNs - lastSignalNs < cooldownNs) {
            return;
        }
        // Peak confirmed: dropped confirmTicks from run high
        if (runHi - tick >= confirmTicks) {
            if (armIfHighVol(Side.SHORT, runHi, tNs, tick, size)) {
                return;
            }
            // Failed HV check — reset high so we don't re-test forever
            runHi = tick;
            runHiNs = tNs;
        }
        // Trough confirmed: rose confirmTicks from run low
        if (tick - runLo >= confirmTicks) {
            if (armIfHighVol(Side.LONG, runLo, tNs, tick, size)) {
                return;
            }
            runLo = tick;
            runLoNs = tNs;
        }
    }

    private boolean armIfHighVol(Side side, int extreme, long tNs, int tick, int size) {
        double avg = avgVolPerTick();
        if (avg <= 0) {
            return false;
        }
        int half = extremeBandTicks;
        double vol = volumeInRange(extreme - half, extreme + half);
        int width = Math.max(1, 2 * half + 1);
        double ratio = vol / (avg * width);
        if (ratio < highVolMult) {
            return false;
        }
        watching = side;
        extremeTick = extreme;
        extremeVol = vol;
        extremeVolRatio = ratio;
        armNs = tNs;
        pathLastTick = tick;
        pathLengthTicks = Math.abs(tick - extreme);
        maxPullbackTicks = 0;
        favorTick = tick;
        legVol = 0;
        // Arming print counts if already outside the peak band.
        if (isOutsidePeakBand(tick)) {
            legVol = size;
        }
        return true;
    }

    private boolean isOutsidePeakBand(int tick) {
        int half = extremeBandTicks;
        if (watching == Side.LONG) {
            return tick > extremeTick + half;
        }
        if (watching == Side.SHORT) {
            return tick < extremeTick - half;
        }
        return false;
    }

    private void updateWatch(long tNs, int tick, int prevTick, int size) {
        if (prevTick != Integer.MIN_VALUE) {
            pathLengthTicks += Math.abs(tick - pathLastTick);
        }
        pathLastTick = tick;

        if (watching == Side.SHORT) {
            if (tick > extremeTick) {
                clearWatch(tNs, tick);
                tryWatch(tNs, tick, size);
                return;
            }
            if (tick < favorTick) {
                favorTick = tick;
            }
            int giveback = tick - favorTick; // bounce up from lows
            if (giveback > maxPullbackTicks) {
                maxPullbackTicks = giveback;
            }
            if (maxLegNs > 0 && tNs - armNs > maxLegNs) {
                clearWatch(tNs, tick);
                return;
            }
            if (maxPullbackTicksAllowed > 0 && maxPullbackTicks > maxPullbackTicksAllowed) {
                clearWatch(tNs, tick);
                return;
            }
            if (extremeTick - tick >= moveTicks) {
                tryFire(tNs, tick);
            }
        } else if (watching == Side.LONG) {
            if (tick < extremeTick) {
                clearWatch(tNs, tick);
                tryWatch(tNs, tick, size);
                return;
            }
            if (tick > favorTick) {
                favorTick = tick;
            }
            int giveback = favorTick - tick; // dip from highs
            if (giveback > maxPullbackTicks) {
                maxPullbackTicks = giveback;
            }
            if (maxLegNs > 0 && tNs - armNs > maxLegNs) {
                clearWatch(tNs, tick);
                return;
            }
            if (maxPullbackTicksAllowed > 0 && maxPullbackTicks > maxPullbackTicksAllowed) {
                clearWatch(tNs, tick);
                return;
            }
            if (tick - extremeTick >= moveTicks) {
                tryFire(tNs, tick);
            }
        }
    }

    private void tryFire(long tNs, int tick) {
        int disp = Math.abs(tick - extremeTick);
        double pathLen = Math.max(1.0, pathLengthTicks);
        double eff = disp / pathLen;
        if (minPathEfficiency > 0 && eff < minPathEfficiency) {
            clearWatch(tNs, tick);
            return;
        }
        double avg = avgVolPerTick();
        double expected = avg * outsideTicks();
        double legRatio = expected > 0 ? legVol / expected : Double.POSITIVE_INFINITY;
        if (maxLegVolVsAvg > 0 && !(legRatio < maxLegVolVsAvg)) {
            clearWatch(tNs, tick);
            return;
        }
        double vsTip = extremeVol > 0 ? legVol / extremeVol : Double.POSITIVE_INFINITY;
        if (maxLegVolVsTip > 0 && !(vsTip < maxLegVolVsTip)) {
            clearWatch(tNs, tick);
            return;
        }
        double legSec = (tNs - armNs) / 1_000_000_000.0;
        pending = new Signal(
                watching,
                extremeTick,
                tick,
                extremeVol,
                avg,
                extremeVolRatio,
                moveTicks,
                eff,
                maxPullbackTicks,
                legSec,
                legVol,
                legRatio,
                vsTip);
        lastSignalNs = tNs;
        watching = Side.NONE;
        runHi = tick;
        runLo = tick;
        runHiNs = tNs;
        runLoNs = tNs;
        resetPath();
    }

    private void clearWatch(long tNs, int tick) {
        watching = Side.NONE;
        runHi = tick;
        runLo = tick;
        runHiNs = tNs;
        runLoNs = tNs;
        resetPath();
    }

    private void resetPath() {
        pathLengthTicks = 0;
        maxPullbackTicks = 0;
        pathLastTick = Integer.MIN_VALUE;
        favorTick = 0;
        armNs = 0;
        extremeVolRatio = 0;
        legVol = 0;
    }

    private double avgVolPerTick() {
        if (volByTick.isEmpty() || totalVol <= 0) {
            return 0;
        }
        int span = Math.max(1, volByTick.lastKey() - volByTick.firstKey() + 1);
        return totalVol / span;
    }

    private double volumeInRange(int lo, int hi) {
        if (hi < lo) {
            int tmp = lo;
            lo = hi;
            hi = tmp;
        }
        double sum = 0;
        Map.Entry<Integer, Double> e = volByTick.ceilingEntry(lo);
        while (e != null && e.getKey() <= hi) {
            sum += e.getValue();
            e = volByTick.higherEntry(e.getKey());
        }
        return sum;
    }

    private void add(long ns, int tick, int size) {
        double s = size;
        prints.addLast(new Print(ns, tick, s));
        volByTick.merge(tick, s, Double::sum);
        totalVol += s;
    }

    private void prune(long nowNs) {
        long cut = nowNs - lookbackNs;
        while (!prints.isEmpty() && prints.peekFirst().ns < cut) {
            Print p = prints.removeFirst();
            totalVol -= p.size;
            Double v = volByTick.get(p.tick);
            if (v == null) {
                continue;
            }
            double next = v - p.size;
            if (next <= 1e-9) {
                volByTick.remove(p.tick);
            } else {
                volByTick.put(p.tick, next);
            }
        }
        if (totalVol < 0) {
            totalVol = 0;
        }
    }
}
