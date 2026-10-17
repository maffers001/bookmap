package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.TreeMap;

/**
 * Trapped size → weak retest → break.
 *
 * <ol>
 *   <li>Confirm a high-volume local peak/trough (peak1).</li>
 *   <li>Require a minimum pullback, then a second swing that fails:
 *       lower high / higher low with band volume ≤ {@code maxRetestVolFrac}
 *       × peak1 volume.</li>
 *   <li>Fire when price breaks {@code breakTicks} beyond peak2
 *       (SHORT after failed high, LONG after failed low).</li>
 * </ol>
 *
 * Distances are in <b>ticks</b> (API price units on this install).
 */
public final class TrappedRetestTracker {

    public enum Side {
        NONE,
        LONG,
        SHORT
    }

    /** 0 idle, 1 waiting for weak retest, 2 waiting for break. */
    public enum Phase {
        IDLE,
        AFTER_PEAK1,
        AFTER_PEAK2
    }

    public static final class Signal {
        public final Side side;
        public final int peak1Tick;
        public final int peak2Tick;
        public final int triggerTick;
        public final double peak1Vol;
        public final double peak2Vol;
        public final double avgVolPerTick;
        public final double peakRatio;
        public final double retestRatio;

        public Signal(
                Side side,
                int peak1Tick,
                int peak2Tick,
                int triggerTick,
                double peak1Vol,
                double peak2Vol,
                double avgVolPerTick,
                double peakRatio,
                double retestRatio) {
            this.side = side;
            this.peak1Tick = peak1Tick;
            this.peak2Tick = peak2Tick;
            this.triggerTick = triggerTick;
            this.peak1Vol = peak1Vol;
            this.peak2Vol = peak2Vol;
            this.avgVolPerTick = avgVolPerTick;
            this.peakRatio = peakRatio;
            this.retestRatio = retestRatio;
        }
    }

    private final int confirmTicks;
    private final int peakBandTicks;
    private final int minPullbackTicks;
    private final int breakTicks;
    private final double highVolMult;
    private final double maxRetestVolFrac;
    private final long lookbackNs;
    private final long cooldownNs;

    private final Deque<TradePrint> prints = new ArrayDeque<>();
    private final TreeMap<Integer, Double> volByTick = new TreeMap<>();
    private double totalVol;

    private int lastTick = Integer.MIN_VALUE;
    private long lastNs;

    private int runHi = Integer.MIN_VALUE / 4;
    private int runLo = Integer.MAX_VALUE / 4;
    private long runHiNs;
    private long runLoNs;

    private Side side = Side.NONE;
    private Phase phase = Phase.IDLE;

    private int peak1Tick;
    private double peak1Vol;
    private double peak1Ratio;
    private boolean pulledBack;

    /** Second-swing extreme while seeking / holding peak2. */
    private int peak2Tick;
    private double peak2Vol;
    private double retestRatio;
    private int swingHi;
    private int swingLo;

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

    public TrappedRetestTracker(
            int confirmTicks,
            int peakBandTicks,
            int minPullbackTicks,
            int breakTicks,
            double highVolMult,
            double maxRetestVolFrac,
            long lookbackNs,
            long cooldownNs) {
        this.confirmTicks = Math.max(1, confirmTicks);
        this.peakBandTicks = Math.max(1, peakBandTicks);
        this.minPullbackTicks = Math.max(1, minPullbackTicks);
        this.breakTicks = Math.max(1, breakTicks);
        this.highVolMult = Math.max(0.1, highVolMult);
        this.maxRetestVolFrac = Math.max(0.05, Math.min(1.0, maxRetestVolFrac));
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

        if (phase == Phase.IDLE) {
            tryPeak1(tNs, tick);
        } else if (phase == Phase.AFTER_PEAK1) {
            updateAfterPeak1(tNs, tick);
        } else {
            updateAfterPeak2(tNs, tick);
        }
    }

    public Signal pollSignal() {
        Signal s = pending;
        pending = null;
        return s;
    }

    public Side side() {
        return side;
    }

    public Phase phase() {
        return phase;
    }

    public int peak1Tick() {
        return peak1Tick;
    }

    public int peak2Tick() {
        return peak2Tick;
    }

    public double currentPeakRatio() {
        return phase == Phase.IDLE ? 0 : peak1Ratio;
    }

    /**
     * Peak2 band volume ÷ peak1 volume. While seeking the retest, uses the
     * live second-swing extreme so the line contrasts loud vs quiet peaks.
     */
    public double currentRetestRatio() {
        if (phase == Phase.IDLE || peak1Vol <= 0) {
            return 0;
        }
        if (phase == Phase.AFTER_PEAK2) {
            return retestRatio;
        }
        if (!pulledBack) {
            return 0;
        }
        int extreme = side == Side.SHORT ? swingHi : swingLo;
        if (extreme == Integer.MIN_VALUE / 4 || extreme == Integer.MAX_VALUE / 4) {
            return 0;
        }
        return bandVol(extreme) / peak1Vol;
    }

    private void tryPeak1(long tNs, int tick) {
        if (tNs - lastSignalNs < cooldownNs) {
            return;
        }
        if (runHi - tick >= confirmTicks) {
            if (armPeak1(Side.SHORT, runHi, tNs)) {
                return;
            }
            runHi = tick;
            runHiNs = tNs;
        }
        if (tick - runLo >= confirmTicks) {
            if (armPeak1(Side.LONG, runLo, tNs)) {
                return;
            }
            runLo = tick;
            runLoNs = tNs;
        }
    }

    private boolean armPeak1(Side s, int extreme, long tNs) {
        double avg = avgVolPerTick();
        if (avg <= 0) {
            return false;
        }
        double vol = bandVol(extreme);
        double expected = avg * Math.max(1, peakBandTicks);
        double ratio = vol / expected;
        if (ratio < highVolMult) {
            return false;
        }
        side = s;
        phase = Phase.AFTER_PEAK1;
        peak1Tick = extreme;
        peak1Vol = vol;
        peak1Ratio = ratio;
        peak2Tick = extreme;
        peak2Vol = 0;
        retestRatio = 0;
        pulledBack = false;
        swingHi = Integer.MIN_VALUE / 4;
        swingLo = Integer.MAX_VALUE / 4;
        return true;
    }

    private void updateAfterPeak1(long tNs, int tick) {
        if (side == Side.SHORT) {
            if (tick > peak1Tick) {
                clearToIdle(tick, tNs);
                tryPeak1(tNs, tick);
                return;
            }
            if (peak1Tick - tick >= minPullbackTicks) {
                pulledBack = true;
            }
            if (!pulledBack) {
                return;
            }
            if (tick >= swingHi) {
                swingHi = tick;
            }
            if (swingHi - tick >= confirmTicks && swingHi < peak1Tick) {
                tryAcceptPeak2(tNs, swingHi);
            }
        } else {
            if (tick < peak1Tick) {
                clearToIdle(tick, tNs);
                tryPeak1(tNs, tick);
                return;
            }
            if (tick - peak1Tick >= minPullbackTicks) {
                pulledBack = true;
            }
            if (!pulledBack) {
                return;
            }
            if (tick <= swingLo) {
                swingLo = tick;
            }
            if (tick - swingLo >= confirmTicks && swingLo > peak1Tick) {
                tryAcceptPeak2(tNs, swingLo);
            }
        }
    }

    private void tryAcceptPeak2(long tNs, int extreme) {
        double vol = bandVol(extreme);
        double frac = peak1Vol > 0 ? vol / peak1Vol : 1.0;
        if (frac > maxRetestVolFrac) {
            // Too much size on the retest — keep seeking another swing
            if (side == Side.SHORT) {
                swingHi = lastTick;
            } else {
                swingLo = lastTick;
            }
            return;
        }
        phase = Phase.AFTER_PEAK2;
        peak2Tick = extreme;
        peak2Vol = vol;
        retestRatio = frac;
    }

    private void updateAfterPeak2(long tNs, int tick) {
        if (side == Side.SHORT) {
            if (tick >= peak1Tick) {
                clearToIdle(tick, tNs);
                tryPeak1(tNs, tick);
                return;
            }
            if (tick > peak2Tick) {
                // Higher retest still below peak1: re-check volume
                double vol = bandVol(tick);
                double frac = peak1Vol > 0 ? vol / peak1Vol : 1.0;
                if (frac > maxRetestVolFrac) {
                    // Became a louder high — fall back to seeking retest
                    phase = Phase.AFTER_PEAK1;
                    pulledBack = true;
                    swingHi = tick;
                    peak2Vol = 0;
                    retestRatio = frac;
                    return;
                }
                peak2Tick = tick;
                peak2Vol = vol;
                retestRatio = frac;
            }
            if (peak2Tick - tick >= breakTicks) {
                fire(tNs, tick);
            }
        } else {
            if (tick <= peak1Tick) {
                clearToIdle(tick, tNs);
                tryPeak1(tNs, tick);
                return;
            }
            if (tick < peak2Tick) {
                double vol = bandVol(tick);
                double frac = peak1Vol > 0 ? vol / peak1Vol : 1.0;
                if (frac > maxRetestVolFrac) {
                    phase = Phase.AFTER_PEAK1;
                    pulledBack = true;
                    swingLo = tick;
                    peak2Vol = 0;
                    retestRatio = frac;
                    return;
                }
                peak2Tick = tick;
                peak2Vol = vol;
                retestRatio = frac;
            }
            if (tick - peak2Tick >= breakTicks) {
                fire(tNs, tick);
            }
        }
    }

    private void fire(long tNs, int tick) {
        pending = new Signal(
                side,
                peak1Tick,
                peak2Tick,
                tick,
                peak1Vol,
                peak2Vol,
                avgVolPerTick(),
                peak1Ratio,
                retestRatio);
        lastSignalNs = tNs;
        clearToIdle(tick, tNs);
    }

    private void clearToIdle(int tick, long tNs) {
        side = Side.NONE;
        phase = Phase.IDLE;
        pulledBack = false;
        peak1Vol = 0;
        peak1Ratio = 0;
        peak2Vol = 0;
        retestRatio = 0;
        swingHi = Integer.MIN_VALUE / 4;
        swingLo = Integer.MAX_VALUE / 4;
        runHi = tick;
        runLo = tick;
        runHiNs = tNs;
        runLoNs = tNs;
    }

    private double bandVol(int extreme) {
        int half = peakBandTicks / 2;
        return volumeInRange(extreme - half, extreme + half);
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
