package com.bookmap.orderflow.core;

/**
 * 22-tick range Cumulative Delta with SMA(100) and Bollinger, matching
 * discretionary CD + research EXP-0009 pullback gate.
 *
 * Price is expected in instrument price units; tickSize should be instrument pips
 * (e.g. 0.25 for NQ) so range = rangeTicks * tickSize.
 */
public final class CumulativeDeltaRange {

    private final double rangePts;
    private final int smaLen;
    private final int slopeLookback;
    private final double pullbackFracOfBb;
    private final double bbStd;

    private double o, h, l, c;
    private double buyVol, sellVol;
    private boolean barOpen;

    private final double[] cdRing;
    private int cdCount;
    private int cdHead;
    private double cdSum;
    private double lastCd;
    private double lastSma = Double.NaN;
    private double lastBbWidth = Double.NaN;
    private double smaSlope = Double.NaN;
    private int maTrend; // +1 / -1 / 0
    private boolean pullbackLong;
    private boolean pullbackShort;

    // ring of recent SMA for slope
    private final double[] smaRing;
    private int smaCount;
    private int smaHead;

    public CumulativeDeltaRange(int rangeTicks, double tickSize, int smaLen,
                                int slopeLookback, double pullbackFracOfBb) {
        this.rangePts = Math.max(1, rangeTicks) * tickSize;
        this.smaLen = Math.max(10, smaLen);
        this.slopeLookback = Math.max(1, slopeLookback);
        this.pullbackFracOfBb = pullbackFracOfBb;
        this.bbStd = 2.0;
        this.cdRing = new double[this.smaLen];
        this.smaRing = new double[this.slopeLookback + 2];
    }

    public void onTrade(double price, int size, boolean buyAggressor) {
        if (!barOpen) {
            o = h = l = c = price;
            buyVol = sellVol = 0;
            barOpen = true;
        }
        if (buyAggressor) {
            buyVol += size;
        } else {
            sellVol += size;
        }
        h = Math.max(h, price);
        l = Math.min(l, price);
        c = price;
        if (h - l >= rangePts) {
            closeBar();
            o = h = l = c = price;
            buyVol = sellVol = 0;
            barOpen = true;
        }
    }

    private void closeBar() {
        double delta = buyVol - sellVol;
        lastCd += delta;
        pushCd(lastCd);
        updateSmaBb();
        updateTrendAndPullback();
    }

    private void pushCd(double cd) {
        if (cdCount < cdRing.length) {
            cdRing[cdCount++] = cd;
            cdSum += cd;
        } else {
            cdSum -= cdRing[cdHead];
            cdRing[cdHead] = cd;
            cdHead = (cdHead + 1) % cdRing.length;
            cdSum += cd;
        }
    }

    private void updateSmaBb() {
        int n = cdCount;
        if (n < Math.max(10, smaLen / 4)) {
            lastSma = Double.NaN;
            lastBbWidth = Double.NaN;
            return;
        }
        double mean = cdSum / n;
        double var = 0;
        for (int i = 0; i < n; i++) {
            double d = cdRing[i] - mean;
            var += d * d;
        }
        double sd = Math.sqrt(var / n);
        lastSma = mean;
        lastBbWidth = 2 * bbStd * sd;
        pushSma(mean);
    }

    private void pushSma(double sma) {
        if (smaCount < smaRing.length) {
            smaRing[smaCount++] = sma;
        } else {
            smaRing[smaHead] = sma;
            smaHead = (smaHead + 1) % smaRing.length;
        }
    }

    private double smaAtOffset(int barsAgo) {
        if (smaCount <= barsAgo) {
            return Double.NaN;
        }
        if (smaCount < smaRing.length) {
            return smaRing[smaCount - 1 - barsAgo];
        }
        int idx = (smaHead - 1 - barsAgo) % smaRing.length;
        if (idx < 0) {
            idx += smaRing.length;
        }
        return smaRing[idx];
    }

    private void updateTrendAndPullback() {
        pullbackLong = false;
        pullbackShort = false;
        maTrend = 0;
        smaSlope = Double.NaN;
        if (Double.isNaN(lastSma) || Double.isNaN(lastBbWidth) || lastBbWidth <= 0) {
            return;
        }
        double past = smaAtOffset(slopeLookback);
        if (!Double.isNaN(past)) {
            smaSlope = lastSma - past;
            if (smaSlope > 0) {
                maTrend = 1;
            } else if (smaSlope < 0) {
                maTrend = -1;
            }
        }
        double dist = lastCd - lastSma;
        double thr = pullbackFracOfBb * lastBbWidth;
        boolean near = Math.abs(dist) <= thr;
        if (maTrend > 0 && near && dist <= 0) {
            pullbackLong = true;
        }
        if (maTrend < 0 && near && dist >= 0) {
            pullbackShort = true;
        }
    }

    public int getMaTrend() {
        return maTrend;
    }

    public boolean isPullbackLong() {
        return pullbackLong;
    }

    public boolean isPullbackShort() {
        return pullbackShort;
    }

    public double getCd() {
        return lastCd;
    }

    public double getSma() {
        return lastSma;
    }

    public void reset() {
        barOpen = false;
        buyVol = sellVol = 0;
        o = h = l = c = 0;
        cdCount = cdHead = 0;
        cdSum = 0;
        lastCd = 0;
        lastSma = Double.NaN;
        lastBbWidth = Double.NaN;
        smaSlope = Double.NaN;
        maTrend = 0;
        pullbackLong = pullbackShort = false;
        smaCount = smaHead = 0;
        for (int i = 0; i < cdRing.length; i++) {
            cdRing[i] = 0;
        }
        for (int i = 0; i < smaRing.length; i++) {
            smaRing[i] = 0;
        }
    }
}
