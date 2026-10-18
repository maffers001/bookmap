package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Causal 1-minute regime features matching {@code research/features/regime.py}.
 *
 * Prices are chart points. <b>No hindsight:</b>
 * <ul>
 *   <li>Trend / range windows use only <em>completed</em> 1-minute bars
 *       (minute end &le; asof) — the unfinished minute (and the burst's own
 *       move inside it) does not define the trend.</li>
 *   <li>VWAP is a running trade VWAP of prints seen so far (not rebuilt from
 *       finished bar closes).</li>
 *   <li>{@code lastPx} is the last trade price fed at/before asof.</li>
 * </ul>
 */
public final class RegimeTracker {

    public static final class Snapshot {
        public final double rangePts;
        public final double netPts;
        public final double efficiencyRatio;
        public final double vwap;
        public final double medianMinuteRange;
        public final double lastPx;
        public final int nBars;

        Snapshot(double rangePts, double netPts, double efficiencyRatio,
                 double vwap, double medianMinuteRange, double lastPx, int nBars) {
            this.rangePts = rangePts;
            this.netPts = netPts;
            this.efficiencyRatio = efficiencyRatio;
            this.vwap = vwap;
            this.medianMinuteRange = medianMinuteRange;
            this.lastPx = lastPx;
            this.nBars = nBars;
        }

        public static Snapshot empty() {
            return new Snapshot(0, 0, 0, Double.NaN, 0, Double.NaN, 0);
        }
    }

    private static final class MinuteBar {
        final long tsNs;
        double open;
        double high;
        double low;
        double close;
        double volume;

        MinuteBar(long tsNs, double px, double vol) {
            this.tsNs = tsNs;
            this.open = this.high = this.low = this.close = px;
            this.volume = vol;
        }

        void update(double px, double vol) {
            high = Math.max(high, px);
            low = Math.min(low, px);
            close = px;
            volume += vol;
        }

        long endNs() {
            return tsNs + MINUTE_NS;
        }
    }

    private static final long MINUTE_NS = 60_000_000_000L;

    private final ArrayDeque<MinuteBar> bars = new ArrayDeque<>();
    private MinuteBar current;
    private long sessionVwapFromNs = -1;
    private double sessionPv; // sum px*vol of trades since session open
    private double sessionVol;
    private double lastTradePx = Double.NaN;

    private final int maxBars;

    public RegimeTracker() {
        this(180);
    }

    public RegimeTracker(int maxBars) {
        this.maxBars = Math.max(60, maxBars);
    }

    /**
     * Mark RTH / session open for VWAP. Resets the running trade VWAP; does
     * <b>not</b> rebuild from bar closes (that would use end-of-minute prices
     * not known mid-bar). Subsequent {@link #onTrade} calls re-accumulate.
     */
    public void markSessionOpen(long tsNs) {
        sessionVwapFromNs = tsNs;
        sessionPv = 0;
        sessionVol = 0;
    }

    public void reset() {
        bars.clear();
        current = null;
        sessionVwapFromNs = -1;
        sessionPv = 0;
        sessionVol = 0;
        lastTradePx = Double.NaN;
    }

    /** Feed a trade (chart points). */
    public void onTrade(long tsNs, double pricePts, int size) {
        if (Double.isNaN(pricePts) || size <= 0) {
            return;
        }
        lastTradePx = pricePts;
        long slot = (tsNs / MINUTE_NS) * MINUTE_NS;
        if (current == null) {
            current = new MinuteBar(slot, pricePts, size);
            addToVwap(pricePts, size, tsNs);
            return;
        }
        if (slot != current.tsNs) {
            bars.addLast(current);
            while (bars.size() > maxBars) {
                bars.removeFirst();
            }
            current = new MinuteBar(slot, pricePts, size);
            addToVwap(pricePts, size, tsNs);
        } else {
            current.update(pricePts, size);
            addToVwap(pricePts, size, tsNs);
        }
    }

    private void addToVwap(double px, double vol, long tradeTsNs) {
        // Only after markSessionOpen — before that VWAP is NaN and the gate is skipped
        // (matches research vwap_from_ns = UK 14:30; avoids premkt hindsight).
        if (sessionVwapFromNs >= 0 && tradeTsNs >= sessionVwapFromNs) {
            sessionPv += px * vol;
            sessionVol += vol;
        }
    }

    public Snapshot snapshot(int windowMin) {
        // Without exchange asof, only use already-closed bars (exclude current).
        return snapshotClosedOnly(windowMin);
    }

    private Snapshot snapshotClosedOnly(int windowMin) {
        List<MinuteBar> closed = new ArrayList<>(bars.size());
        closed.addAll(bars);
        if (closed.isEmpty()) {
            double vwap = sessionVol > 0 ? sessionPv / sessionVol : Double.NaN;
            return new Snapshot(0, 0, 0, vwap, 0, lastTradePx, 0);
        }
        return snapshotFromBars(closed, windowMin);
    }

    private Snapshot snapshotFromBars(List<MinuteBar> closedIn, int windowMin) {
        if (closedIn.isEmpty()) {
            double vwap = sessionVol > 0 ? sessionPv / sessionVol : Double.NaN;
            return new Snapshot(0, 0, 0, vwap, 0, lastTradePx, 0);
        }
        int w = Math.max(1, windowMin);
        int from = Math.max(0, closedIn.size() - w);
        List<MinuteBar> closed = closedIn.subList(from, closedIn.size());
        double hi = Double.NEGATIVE_INFINITY;
        double lo = Double.POSITIVE_INFINITY;
        double[] ranges = new double[closed.size()];
        for (int i = 0; i < closed.size(); i++) {
            MinuteBar b = closed.get(i);
            hi = Math.max(hi, b.high);
            lo = Math.min(lo, b.low);
            ranges[i] = b.high - b.low;
        }
        double net = closed.get(closed.size() - 1).close - closed.get(0).open;
        double path = 0;
        if (closed.size() == 1) {
            path = Math.abs(closed.get(0).close - closed.get(0).open);
        } else {
            for (int i = 1; i < closed.size(); i++) {
                path += Math.abs(closed.get(i).close - closed.get(i - 1).close);
            }
        }
        double er = path > 1e-9 ? Math.abs(net) / path : 0.0;
        Arrays.sort(ranges);
        double med = ranges[ranges.length / 2];
        if (ranges.length % 2 == 0 && ranges.length >= 2) {
            med = 0.5 * (ranges[ranges.length / 2 - 1] + ranges[ranges.length / 2]);
        }
        double vwap = sessionVol > 0 ? sessionPv / sessionVol : Double.NaN;
        double last = !Double.isNaN(lastTradePx) ? lastTradePx : closed.get(closed.size() - 1).close;
        return new Snapshot(hi - lo, net, er, vwap, med, last, closed.size());
    }

    /**
     * @param windowMin number of <em>completed</em> minutes in the rolling window
     * @param asofNs    exchange time — only minutes with end &le; asof are used
     */
    public Snapshot snapshot(int windowMin, long asofNs) {
        List<MinuteBar> closed = new ArrayList<>(bars.size() + 1);
        for (MinuteBar b : bars) {
            if (b.endNs() <= asofNs) {
                closed.add(b);
            }
        }
        // Include current only if that minute has fully ended by asof
        if (current != null && current.endNs() <= asofNs) {
            closed.add(current);
        }
        if (closed.isEmpty()) {
            double vwap = sessionVol > 0 ? sessionPv / sessionVol : Double.NaN;
            return new Snapshot(0, 0, 0, vwap, 0, lastTradePx, 0);
        }
        return snapshotFromBars(closed, windowMin);
    }
}
