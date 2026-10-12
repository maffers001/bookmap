package com.bookmap.orderflow.core;

/** Simple exponential moving average. */
public final class Ema {
    private final double alpha;
    private double value;
    private boolean seeded;

    public Ema(int periods) {
        if (periods < 1) {
            throw new IllegalArgumentException("periods must be >= 1");
        }
        this.alpha = 2.0 / (periods + 1.0);
    }

    public double update(double sample) {
        if (!seeded) {
            value = sample;
            seeded = true;
        } else {
            value = alpha * sample + (1.0 - alpha) * value;
        }
        return value;
    }

    public double get() {
        return value;
    }

    public boolean isSeeded() {
        return seeded;
    }

    public void reset() {
        value = 0;
        seeded = false;
    }
}
