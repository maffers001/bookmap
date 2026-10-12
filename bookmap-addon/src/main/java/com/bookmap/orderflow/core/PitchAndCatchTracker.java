package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.TreeMap;

/**
 * Pitch-and-catch: high volume at a rotation peak/trough ("pitch"), then a
 * configurable low-volume move away ("catch").
 *
 * Low volume on the catch leg: sum of traded volume on the price ticks covering
 * the move is below {@code avgVolPerTick × catchTicks × lowVolFrac}, where
 * avgVolPerTick is total lookback volume / (profile high − low + 1).
 *
 * High volume at the extreme: volume in a band around the peak/trough is at
 * least {@code avgVolPerTick × bandWidth × highVolMult}.
 */
public final class PitchAndCatchTracker {

    public enum Side {
        NONE,
        LONG,   // trough pitch → catch up
        SHORT   // peak pitch → catch down
    }

    public static final class Signal {
        public final Side side;
        public final int pitchTick;
        public final int catchTick;
        public final double peakVol;
        public final double pathVol;
        public final double avgVolPerTick;
        public final double peakRatio; // peakVol / (avg * band)
        public final double pathRatio; // pathVol / (avg * catch)

        public Signal(
                Side side,
                int pitchTick,
                int catchTick,
                double peakVol,
                double pathVol,
                double avgVolPerTick,
                double peakRatio,
                double pathRatio) {
            this.side = side;
            this.pitchTick = pitchTick;
            this.catchTick = catchTick;
            this.peakVol = peakVol;
            this.pathVol = pathVol;
            this.avgVolPerTick = avgVolPerTick;
            this.peakRatio = peakRatio;
            this.pathRatio = pathRatio;
        }
    }

    private final int catchTicks;
    private final int peakBandTicks;
    private final int confirmTicks;
    private final int reclaimTicks;
    private final double highVolMult;
    private final double lowVolFrac;
    private final long lookbackNs;
    private final long cooldownNs;

    private final Deque<TradePrint> prints = new ArrayDeque<>();
    private final TreeMap<Integer, Double> volByTick = new TreeMap<>();
    private double totalVol;

    private int lastTick = Integer.MIN_VALUE;
    private long lastNs;

    /** Running swing extreme while seeking a pitch. */
    private int runHi = Integer.MIN_VALUE / 4;
    private int runLo = Integer.MAX_VALUE / 4;
    private long runHiNs;
    private long runLoNs;

    private Side armed = Side.NONE;
    private int pitchTick;
    private long pitchNs;
    private boolean leftPitch;

    private long lastSignalNs = Long.MIN_VALUE / 4;
    private Signal pending;

    private static final class TradePrint {
        final long ns;
        final int tick;
        final double size;

        TradePrint(long ns, int tick, double size) {
            this.ns = ns;
            this.tick = tick;
            this.size = size;
        }
    }

    public PitchAndCatchTracker(
            int catchTicks,
            int peakBandTicks,
            int confirmTicks,
            int reclaimTicks,
            double highVolMult,
            double lowVolFrac,
            long lookbackNs,
            long cooldownNs) {
        this.catchTicks = Math.max(2, catchTicks);
        this.peakBandTicks = Math.max(1, peakBandTicks);
        this.confirmTicks = Math.max(1, confirmTicks);
        this.reclaimTicks = Math.max(0, reclaimTicks);
        this.highVolMult = Math.max(0.1, highVolMult);
        this.lowVolFrac = Math.max(0.05, lowVolFrac);
        this.lookbackNs = Math.max(1_000_000_000L, lookbackNs);
        this.cooldownNs = Math.max(0L, cooldownNs);
    }

    public void onTrade(long tNs, int tick, int size) {
        if (size <= 0) {
            return;
        }
        lastNs = tNs;
        lastTick = tick;
        addPrint(tNs, tick, size);
        prune(tNs);

        if (tick >= runHi) {
            runHi = tick;
            runHiNs = tNs;
        }
        if (tick <= runLo) {
            runLo = tick;
            runLoNs = tNs;
        }

        if (armed == Side.NONE) {
            tryArm(tNs, tick);
        } else {
            updateArmed(tNs, tick);
        }
    }

    /** Non-null once when a catch completes; cleared on next poll. */
    public Signal pollSignal() {
        Signal s = pending;
        pending = null;
        return s;
    }

    public Side armedSide() {
        return armed;
    }

    public int pitchTick() {
        return pitchTick;
    }

    /** Catch distance in ticks (same units as {@link #pitchTick()}). */
    public int catchTicks() {
        return catchTicks;
    }

    public double avgVolPerTick() {
        return computeAvgVolPerTick();
    }

    public double currentPeakRatio() {
        if (armed == Side.NONE || lastTick == Integer.MIN_VALUE) {
            return 0;
        }
        double avg = computeAvgVolPerTick();
        if (avg <= 0) {
            return 0;
        }
        int half = peakBandTicks / 2;
        double peakVol = volumeInRange(pitchTick - half, pitchTick + half);
        return peakVol / (avg * Math.max(1, peakBandTicks));
    }

    public double currentPathRatio() {
        if (armed == Side.NONE || lastTick == Integer.MIN_VALUE) {
            return Double.NaN;
        }
        double avg = computeAvgVolPerTick();
        if (avg <= 0) {
            return Double.NaN;
        }
        int span = Math.min(catchTicks, Math.abs(lastTick - pitchTick));
        if (span <= 0) {
            return Double.NaN;
        }
        int lo;
        int hi;
        if (armed == Side.SHORT) {
            hi = pitchTick - 1;
            lo = pitchTick - span;
        } else {
            lo = pitchTick + 1;
            hi = pitchTick + span;
        }
        double pathVol = volumeInRange(lo, hi);
        return pathVol / (avg * span);
    }

    private void tryArm(long tNs, int tick) {
        // Confirm a high: price has dropped confirmTicks from runHi
        if (runHi - tick >= confirmTicks) {
            arm(Side.SHORT, runHi, runHiNs, tNs);
            return;
        }
        // Confirm a low: price has risen confirmTicks from runLo
        if (tick - runLo >= confirmTicks) {
            arm(Side.LONG, runLo, runLoNs, tNs);
        }
    }

    private void arm(Side side, int extreme, long extremeNs, long nowNs) {
        if (nowNs - lastSignalNs < cooldownNs) {
            return;
        }
        double avg = computeAvgVolPerTick();
        if (avg <= 0) {
            return;
        }
        int half = peakBandTicks / 2;
        double peakVol = volumeInRange(extreme - half, extreme + half);
        double expected = avg * Math.max(1, peakBandTicks);
        if (peakVol < expected * highVolMult) {
            // Not a high-volume pitch — reset that extreme and keep seeking
            if (side == Side.SHORT) {
                runHi = lastTick;
                runHiNs = nowNs;
            } else {
                runLo = lastTick;
                runLoNs = nowNs;
            }
            return;
        }
        armed = side;
        pitchTick = extreme;
        pitchNs = extremeNs;
        leftPitch = false;
    }

    private void updateArmed(long tNs, int tick) {
        if (armed == Side.SHORT) {
            if (tick > pitchTick) {
                // New high — re-check as potential higher pitch
                runHi = tick;
                runHiNs = tNs;
                armed = Side.NONE;
                tryArm(tNs, tick);
                return;
            }
            if (pitchTick - tick >= 3) {
                leftPitch = true;
            }
            if (leftPitch && tick >= pitchTick - reclaimTicks) {
                cancelArm(tick, tNs);
                return;
            }
            if (pitchTick - tick >= catchTicks) {
                maybeFire(tNs, tick);
            }
        } else if (armed == Side.LONG) {
            if (tick < pitchTick) {
                runLo = tick;
                runLoNs = tNs;
                armed = Side.NONE;
                tryArm(tNs, tick);
                return;
            }
            if (tick - pitchTick >= 3) {
                leftPitch = true;
            }
            if (leftPitch && tick <= pitchTick + reclaimTicks) {
                cancelArm(tick, tNs);
                return;
            }
            if (tick - pitchTick >= catchTicks) {
                maybeFire(tNs, tick);
            }
        }
    }

    private void cancelArm(int tick, long tNs) {
        armed = Side.NONE;
        runHi = tick;
        runLo = tick;
        runHiNs = tNs;
        runLoNs = tNs;
    }

    private void maybeFire(long tNs, int tick) {
        double avg = computeAvgVolPerTick();
        if (avg <= 0) {
            cancelArm(tick, tNs);
            return;
        }
        int half = peakBandTicks / 2;
        double peakVol = volumeInRange(pitchTick - half, pitchTick + half);
        double peakExpected = avg * Math.max(1, peakBandTicks);
        double peakRatio = peakVol / peakExpected;

        int lo;
        int hi;
        if (armed == Side.SHORT) {
            hi = pitchTick - 1;
            lo = pitchTick - catchTicks;
        } else {
            lo = pitchTick + 1;
            hi = pitchTick + catchTicks;
        }
        double pathVol = volumeInRange(lo, hi);
        double pathExpected = avg * catchTicks;
        double pathRatio = pathVol / pathExpected;

        if (peakRatio >= highVolMult && pathRatio <= lowVolFrac) {
            pending = new Signal(
                    armed, pitchTick, tick, peakVol, pathVol, avg, peakRatio, pathRatio);
            lastSignalNs = tNs;
            cancelArm(tick, tNs);
            runHi = tick;
            runLo = tick;
            runHiNs = tNs;
            runLoNs = tNs;
        } else {
            cancelArm(tick, tNs);
        }
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

    private double computeAvgVolPerTick() {
        if (volByTick.isEmpty() || totalVol <= 0) {
            return 0;
        }
        int lo = volByTick.firstKey();
        int hi = volByTick.lastKey();
        int span = Math.max(1, hi - lo + 1);
        return totalVol / span;
    }

    private void addPrint(long ns, int tick, int size) {
        double s = size;
        prints.addLast(new TradePrint(ns, tick, s));
        volByTick.merge(tick, s, Double::sum);
        totalVol += s;
    }

    private void prune(long nowNs) {
        long cut = nowNs - lookbackNs;
        while (!prints.isEmpty() && prints.peekFirst().ns < cut) {
            TradePrint p = prints.removeFirst();
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
