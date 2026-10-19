package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Iterator;

import com.bookmap.orderflow.core.TapeBurstTracker.Burst;

/**
 * Correlated exhaustion fade detector (H29 / EXP-0036_DIG4), Bookmap-free so
 * it can be replayed offline against research events.
 *
 * <p>Pipeline, all on fixed wall-clock tape slots:
 * <ol>
 *   <li>NQ and ES {@link TapeBurstTracker} bursts (EXP-0008 rules).</li>
 *   <li><b>Confirm</b> pairing: ES same-side burst within ±sync and |lag| ≤
 *       max lag. ES at/before NQ → stamp at NQ bar end; NQ-first → held until
 *       ES arrives, stamp at ES bar end (research {@code stamp="confirm"} =
 *       {@code max(nq_end, es_end)}).</li>
 *   <li>From the stamp, walk <em>complete</em> slots (≤ fade max wait) for the
 *       first bar where NQ still pushes with the burst but ES is against
 *       ({@code side×es_imb ≤ esAgainstImbMax} on a bar with ES prints).
 *       Emit a fade (side = −burst side) stamped at that bar's end.</li>
 * </ol>
 * Two bursts resolving on the same bar emit one fade (research dedupe).
 * Feed trades in exchange-time order and call {@link #onTime(long)} as the
 * clock advances so slots complete even when one leg is quiet.
 */
public final class CorrFadeDetector {

    public static final class Config {
        public int barMs = 100;
        public int minTrades = 6;
        public double minVol = 25.0;
        public double minImbalance = 0.55;
        /** Burst progress threshold in the price units fed to {@code onNqTrade}. */
        public double nqMinProgress = 1.0;
        /** Same for ES prices. */
        public double esMinProgress = 1.0;
        public int cooldownBars = 3;
        public double syncMs = 1500.0;
        public double maxEsLagMs = 250.0;
        public double fadeMaxWaitSec = 5.0;
        public double nqStillMinImb = 0.55;
        public double nqStillMinVol = 25.0;
        public double nqStillMinTrades = 6.0;
        public double esAgainstImbMax = -0.4;
        public double esAgainstMinVol = 0.0;
    }

    public static final class FadeEvent {
        public final int side;
        public final long stampNs;
        public final int burstSide;
        public final long burstStampNs;
        public final long nqBurstTsNs;
        public final long esBurstTsNs;
        public final long lagMs;
        public final double waitMs;
        public final double nqClose;
        public final double nqImbX;
        public final double nqVolX;
        public final int nqTradesX;
        public final double esImbX;
        public final double esVolX;
        public final double burstVol;
        public final double burstImb;
        /** Processing clock − stamp when the fade was recognised (lazy bar closes). */
        public final double detectDelayMs;

        FadeEvent(PendingFade p, long stampNs, TapeBarSeries.Bar nb, TapeBarSeries.Bar eb, long nowNs) {
            this.detectDelayMs = (nowNs - stampNs) / 1e6;
            this.side = -p.burstSide;
            this.stampNs = stampNs;
            this.burstSide = p.burstSide;
            this.burstStampNs = p.stampNs;
            this.nqBurstTsNs = p.nqTsNs;
            this.esBurstTsNs = p.esTsNs;
            this.lagMs = p.lagMs;
            this.waitMs = (stampNs - p.stampNs) / 1e6;
            this.nqClose = nb.close;
            this.nqImbX = nb.imbalance();
            this.nqVolX = nb.totalVol();
            this.nqTradesX = nb.nTrades;
            this.esImbX = eb.imbalance();
            this.esVolX = eb.totalVol();
            this.burstVol = p.burstVol;
            this.burstImb = p.burstImb;
        }
    }

    public interface Listener {
        default void onNqBurst(Burst b, boolean pendingEs) {}

        default void onEsBurst(Burst b) {}

        default void onConfirm(int side, long stampNs, long nqTsNs, long esTsNs, long lagMs) {}

        default void onExpire(int burstSide, long stampNs) {}

        void onFade(FadeEvent e);
    }

    private static final class PendingFade {
        final int burstSide;
        final long stampNs;
        final long nqTsNs;
        final long esTsNs;
        final long lagMs;
        final double burstVol;
        final double burstImb;
        long nextSlotNs;

        PendingFade(int burstSide, long stampNs, long nqTsNs, long esTsNs, long lagMs, Burst nq) {
            this.burstSide = burstSide;
            this.stampNs = stampNs;
            this.nqTsNs = nqTsNs;
            this.esTsNs = esTsNs;
            this.lagMs = lagMs;
            this.burstVol = nq != null ? nq.totalVol : 0.0;
            this.burstImb = nq != null ? nq.imbalance : 0.0;
            this.nextSlotNs = stampNs;
        }
    }

    private final Config cfg;
    private final Listener listener;
    private final TapeBurstTracker nqBurst;
    private final TapeBurstTracker esBurst;
    private final TapeBarSeries nqBars;
    private final TapeBarSeries esBars;
    private final long barNs;
    private final long syncNs;
    private final long maxLagNs;
    private final long waitNs;

    /**
     * Bars close lazily (on the next print of that instrument), so a burst can
     * become known well after its stamp. Pairing is therefore done on bar
     * <em>stamps</em> against short histories of both legs, with a grace period
     * on the processing clock before an unconfirmed NQ burst is dropped.
     */
    private final ArrayDeque<Burst> esHistory = new ArrayDeque<>();
    private final ArrayDeque<Burst> nqUnconfirmed = new ArrayDeque<>();
    private final ArrayDeque<PendingFade> pending = new ArrayDeque<>();
    private long lastFadeStampNs = -1;

    private long nqBurstCount;
    private long esBurstCount;
    private long confirmCount;
    private long fadeCount;

    public CorrFadeDetector(Config cfg, Listener listener) {
        this.cfg = cfg;
        this.listener = listener;
        int bar = Math.max(10, cfg.barMs);
        this.barNs = bar * 1_000_000L;
        this.nqBurst = new TapeBurstTracker(bar, cfg.minTrades, cfg.minVol, cfg.minImbalance,
                cfg.nqMinProgress, cfg.cooldownBars);
        this.esBurst = new TapeBurstTracker(bar, cfg.minTrades, cfg.minVol, cfg.minImbalance,
                cfg.esMinProgress, cfg.cooldownBars);
        int keep = (int) Math.max(100, Math.ceil(cfg.fadeMaxWaitSec * 1000.0 / bar) * 3);
        this.nqBars = new TapeBarSeries(bar, keep);
        this.esBars = new TapeBarSeries(bar, keep);
        this.syncNs = (long) (cfg.syncMs * 1_000_000.0);
        this.maxLagNs = cfg.maxEsLagMs > 0 ? (long) (cfg.maxEsLagMs * 1_000_000.0) : syncNs;
        this.waitNs = (long) (cfg.fadeMaxWaitSec * 1_000_000_000.0);
    }

    public long nqBurstCount() {
        return nqBurstCount;
    }

    public long esBurstCount() {
        return esBurstCount;
    }

    public long confirmCount() {
        return confirmCount;
    }

    public long fadeCount() {
        return fadeCount;
    }

    public int pendingFades() {
        return pending.size();
    }

    public Burst lastNqBurst() {
        return nqBurst.getLastBurst();
    }

    public void reset() {
        nqBurst.reset();
        esBurst.reset();
        nqBars.reset();
        esBars.reset();
        esHistory.clear();
        nqUnconfirmed.clear();
        pending.clear();
        lastFadeStampNs = -1;
    }

    /** Burst.tsNs is slot end − 1 ns; research stamps at the slot boundary. */
    private static long stampOf(long burstTsNs) {
        return burstTsNs + 1;
    }

    public void onNqTrade(long tsNs, double price, int size, boolean buyAggressor) {
        nqBars.onTrade(tsNs, price, size, buyAggressor);
        Burst b = nqBurst.onTrade(tsNs, price, size, buyAggressor);
        if (b != null) {
            nqBurstCount++;
            handleNqBurst(b);
        }
        evaluate(tsNs);
    }

    public void onEsTrade(long tsNs, double price, int size, boolean buyAggressor) {
        esBars.onTrade(tsNs, price, size, buyAggressor);
        Burst b = esBurst.onTrade(tsNs, price, size, buyAggressor);
        if (b != null) {
            esBurstCount++;
            listener.onEsBurst(b);
            handleEsBurst(b, tsNs);
        }
        evaluate(tsNs);
    }

    /** Advance the clock without a trade (e.g. 100 ms interval tick). */
    public void onTime(long nowNs) {
        pruneHistories(nowNs);
        evaluate(nowNs);
    }

    /** |es − nq| ≤ max lag (and ≤ sync); research ``confirm`` pairing. */
    private boolean pairs(Burst nq, Burst es) {
        if (es.side != nq.side) {
            return false;
        }
        long absNs = Math.abs(es.tsNs - nq.tsNs);
        return absNs <= syncNs && absNs <= maxLagNs;
    }

    private void handleNqBurst(Burst nq) {
        pruneHistories(nq.tsNs);
        // ES burst already known (either side of the NQ stamp) → confirm now.
        Burst best = null;
        long bestAbs = Long.MAX_VALUE;
        for (Burst es : esHistory) {
            if (pairs(nq, es)) {
                long a = Math.abs(es.tsNs - nq.tsNs);
                if (a < bestAbs) {
                    bestAbs = a;
                    best = es;
                }
            }
        }
        if (best != null) {
            addConfirmed(nq, best);
            listener.onNqBurst(nq, false);
            return;
        }
        nqUnconfirmed.addLast(nq);
        listener.onNqBurst(nq, true);
    }

    private void handleEsBurst(Burst es, long nowNs) {
        pruneHistories(nowNs);
        esHistory.addLast(es);
        // Any unconfirmed NQ burst whose stamp pairs with this ES stamp.
        Iterator<Burst> it = nqUnconfirmed.iterator();
        while (it.hasNext()) {
            Burst nq = it.next();
            if (pairs(nq, es)) {
                it.remove();
                addConfirmed(nq, es);
            }
        }
    }

    /**
     * Drop history older than the pairing window plus a lazy-close grace
     * (one sync window). Pairing itself is stamp-based, so the grace only
     * bounds memory — it cannot admit a pair the research would reject.
     */
    private void pruneHistories(long nowNs) {
        long keepNs = maxLagNs + syncNs;
        while (!esHistory.isEmpty() && nowNs - esHistory.peekFirst().tsNs > keepNs) {
            esHistory.pollFirst();
        }
        while (!nqUnconfirmed.isEmpty() && nowNs - nqUnconfirmed.peekFirst().tsNs > keepNs) {
            nqUnconfirmed.pollFirst();
        }
    }

    private void addConfirmed(Burst nq, Burst es) {
        confirmCount++;
        long lagMs = Math.abs(es.tsNs - nq.tsNs) / 1_000_000L;
        long stamp = stampOf(Math.max(nq.tsNs, es.tsNs));
        pending.addLast(new PendingFade(nq.side, stamp, nq.tsNs, es.tsNs, lagMs, nq));
        listener.onConfirm(nq.side, stamp, nq.tsNs, es.tsNs, lagMs);
    }

    /** Mirrors {@code corr_exhaust._first_exhaust_bar} (use_imb=true, use_vol=false). */
    private void evaluate(long nowNs) {
        if (pending.isEmpty()) {
            return;
        }
        Iterator<PendingFade> it = pending.iterator();
        while (it.hasNext()) {
            PendingFade p = it.next();
            while (nqBars.isComplete(p.nextSlotNs, nowNs)) {
                long slot = p.nextSlotNs;
                if (waitNs > 0 && slot - p.stampNs > waitNs) {
                    it.remove();
                    listener.onExpire(p.burstSide, p.stampNs);
                    break;
                }
                TapeBarSeries.Bar nb = nqBars.get(slot);
                TapeBarSeries.Bar eb = esBars.get(slot);
                boolean nqStill = nb != null
                        && p.burstSide * nb.imbalance() >= cfg.nqStillMinImb
                        && (cfg.nqStillMinVol <= 0 || nb.totalVol() >= cfg.nqStillMinVol)
                        && (cfg.nqStillMinTrades <= 0 || nb.nTrades >= cfg.nqStillMinTrades);
                boolean esAgainst = eb != null
                        && eb.totalVol() > 0
                        && (cfg.esAgainstMinVol <= 0 || eb.totalVol() >= cfg.esAgainstMinVol)
                        && p.burstSide * eb.imbalance() <= cfg.esAgainstImbMax;
                if (nqStill && esAgainst) {
                    long fadeStamp = slot + barNs;
                    it.remove();
                    if (fadeStamp != lastFadeStampNs) {
                        lastFadeStampNs = fadeStamp;
                        fadeCount++;
                        listener.onFade(new FadeEvent(p, fadeStamp, nb, eb, nowNs));
                    }
                    break;
                }
                p.nextSlotNs = slot + barNs;
            }
        }
    }
}
