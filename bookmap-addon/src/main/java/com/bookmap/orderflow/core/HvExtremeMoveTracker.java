package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.TreeMap;

/**
 * Basics: high-volume local peak/trough, then mark when price moves N ticks away.
 *
 * <ol>
 *   <li>Track volume by price tick over a rolling lookback.</li>
 *   <li>When price reverses {@code confirmTicks} from a running high/low, that
 *       extreme is a candidate peak/trough.</li>
 *   <li>Keep it if volume in a small band around the extreme ≥
 *       {@code highVolMult × avgVolPerTick × bandWidth}.</li>
 *   <li>When price is {@code moveTicks} away without making a new extreme,
 *       emit a signal (SHORT after HV peak, LONG after HV trough).</li>
 * </ol>
 *
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

        public Signal(
                Side side,
                int extremeTick,
                int triggerTick,
                double extremeVol,
                double avgVolPerTick,
                double volRatio,
                int moveTicks) {
            this.side = side;
            this.extremeTick = extremeTick;
            this.triggerTick = triggerTick;
            this.extremeVol = extremeVol;
            this.avgVolPerTick = avgVolPerTick;
            this.volRatio = volRatio;
            this.moveTicks = moveTicks;
        }
    }

    private final int moveTicks;
    private final int confirmTicks;
    private final int extremeBandTicks;
    private final double highVolMult;
    private final long lookbackNs;
    private final long cooldownNs;

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
            long cooldownNs) {
        this.moveTicks = Math.max(1, moveTicks);
        this.confirmTicks = Math.max(1, confirmTicks);
        this.extremeBandTicks = Math.max(0, extremeBandTicks);
        this.highVolMult = Math.max(0.1, highVolMult);
        this.lookbackNs = Math.max(1_000_000_000L, lookbackNs);
        this.cooldownNs = Math.max(0L, cooldownNs);
    }

    public void onTrade(long tNs, int tick, int size) {
        if (size <= 0) {
            return;
        }
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
            updateWatch(tNs, tick);
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

    private void tryWatch(long tNs, int tick) {
        if (tNs - lastSignalNs < cooldownNs) {
            return;
        }
        // Peak confirmed: dropped confirmTicks from run high
        if (runHi - tick >= confirmTicks) {
            if (armIfHighVol(Side.SHORT, runHi)) {
                return;
            }
            // Failed HV check — reset high so we don't re-test forever
            runHi = tick;
            runHiNs = tNs;
        }
        // Trough confirmed: rose confirmTicks from run low
        if (tick - runLo >= confirmTicks) {
            if (armIfHighVol(Side.LONG, runLo)) {
                return;
            }
            runLo = tick;
            runLoNs = tNs;
        }
    }

    private boolean armIfHighVol(Side side, int extreme) {
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
        return true;
    }

    private void updateWatch(long tNs, int tick) {
        if (watching == Side.SHORT) {
            if (tick > extremeTick) {
                // New high — drop watch and seek again
                watching = Side.NONE;
                runHi = tick;
                runHiNs = tNs;
                tryWatch(tNs, tick);
                return;
            }
            if (extremeTick - tick >= moveTicks) {
                fire(tNs, tick);
            }
        } else if (watching == Side.LONG) {
            if (tick < extremeTick) {
                watching = Side.NONE;
                runLo = tick;
                runLoNs = tNs;
                tryWatch(tNs, tick);
                return;
            }
            if (tick - extremeTick >= moveTicks) {
                fire(tNs, tick);
            }
        }
    }

    private void fire(long tNs, int tick) {
        pending = new Signal(
                watching,
                extremeTick,
                tick,
                extremeVol,
                avgVolPerTick(),
                extremeVolRatio,
                moveTicks);
        lastSignalNs = tNs;
        watching = Side.NONE;
        runHi = tick;
        runLo = tick;
        runHiNs = tNs;
        runLoNs = tNs;
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
