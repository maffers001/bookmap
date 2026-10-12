package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Deque;

/** Rolling mean / std-dev for z-score normalization. */
public final class RollingStats {
    private final int capacity;
    private final Deque<Double> values = new ArrayDeque<>();
    private double sum;
    private double sumSq;

    public RollingStats(int capacity) {
        if (capacity < 2) {
            throw new IllegalArgumentException("capacity must be >= 2");
        }
        this.capacity = capacity;
    }

    public void add(double value) {
        values.addLast(value);
        sum += value;
        sumSq += value * value;
        if (values.size() > capacity) {
            double removed = values.removeFirst();
            sum -= removed;
            sumSq -= removed * removed;
        }
    }

    public int size() {
        return values.size();
    }

    public double mean() {
        return values.isEmpty() ? 0.0 : sum / values.size();
    }

    public double stdDev() {
        int n = values.size();
        if (n < 2) {
            return 0.0;
        }
        double mean = sum / n;
        double variance = Math.max(0.0, (sumSq / n) - (mean * mean));
        return Math.sqrt(variance);
    }

    public double zScore(double value) {
        double sd = stdDev();
        if (sd < 1e-9) {
            return 0.0;
        }
        return (value - mean()) / sd;
    }

    public void clear() {
        values.clear();
        sum = 0;
        sumSq = 0;
    }
}
