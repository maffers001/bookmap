package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.TreeMap;

/**
 * High-volume local peak/trough, then mark when price moves N ticks away on a
 * <b>clean</b> leg (optional efficiency / max pullback / max time gates).
 *
 * <ol>
 *   <li>Track volume by price tick over a rolling lookback.</li>
 *   <li>When price reverses {@code confirmTicks} from a running high/low, that
 *       extreme is a candidate peak/trough.</li>
 *   <li>Keep it if volume in a small band around the extreme ≥
 *       {@code highVolMult × avgVolPerTick × bandWidth}.</li>
 *   <li>While watching, accumulate path length and max giveback toward the tip.</li>
 *   <li>When price is {@code moveTicks} away, fire only if the leg passes clean
 *       gates (min efficiency, max pullback, max time). Cancel on a new extreme.</li>
 * </ol>
 *
 * All distances are in <b>ticks</b> (API price units on this install).
 * Efficiency / pullback / time gates: {@code 0} disables that gate.
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
                double legSec) {
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
            long maxLegNs) {
        this.moveTicks = Math.max(1, moveTicks);
        this.confirmTicks = Math.max(1, confirmTicks);
        this.extremeBandTicks = Math.max(0, extremeBandTicks);
        this.highVolMult = Math.max(0.1, highVolMult);
        this.lookbackNs = Math.max(1_000_000_000L, lookbackNs);
        this.cooldownNs = Math.max(0L, cooldownNs);
        this.minPathEfficiency = Math.max(0.0, minPathEfficiency);
        this.maxPullbackTicksAllowed = Math.max(0, maxPullbackTicksAllowed);
        this.maxLegNs = Math.max(0L, maxLegNs);
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
            tryWatch(tNs, tick);
        } else {
            updateWatch(tNs, tick, prev);
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

    private void tryWatch(long tNs, int tick) {
        if (tNs - lastSignalNs < cooldownNs) {
            return;
        }
        // Peak confirmed: dropped confirmTicks from run high
        if (runHi - tick >= confirmTicks) {
            if (armIfHighVol(Side.SHORT, runHi, tNs, tick)) {
                return;
            }
            // Failed HV check — reset high so we don't re-test forever
            runHi = tick;
            runHiNs = tNs;
        }
        // Trough confirmed: rose confirmTicks from run low
        if (tick - runLo >= confirmTicks) {
            if (armIfHighVol(Side.LONG, runLo, tNs, tick)) {
                return;
            }
            runLo = tick;
            runLoNs = tNs;
        }
    }

    private boolean armIfHighVol(Side side, int extreme, long tNs, int tick) {
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
        return true;
    }

    private void updateWatch(long tNs, int tick, int prevTick) {
        if (prevTick != Integer.MIN_VALUE) {
            pathLengthTicks += Math.abs(tick - pathLastTick);
        }
        pathLastTick = tick;

        if (watching == Side.SHORT) {
            if (tick > extremeTick) {
                clearWatch(tNs, tick);
                tryWatch(tNs, tick);
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
                tryWatch(tNs, tick);
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
        double legSec = (tNs - armNs) / 1_000_000_000.0;
        pending = new Signal(
                watching,
                extremeTick,
                tick,
                extremeVol,
                avgVolPerTick(),
                extremeVolRatio,
                moveTicks,
                eff,
                maxPullbackTicks,
                legSec);
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
