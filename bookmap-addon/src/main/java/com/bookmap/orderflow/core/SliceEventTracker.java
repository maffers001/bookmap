package com.bookmap.orderflow.core;

import java.util.ArrayDeque;
import java.util.Comparator;
import java.util.Deque;
import java.util.Locale;
import java.util.TreeMap;

/**
 * Shared Liquidity Slice-Through event engine (RULE_SPEC_V1).
 *
 * <p>State machine per armed episode:
 * {@code STACK_ARMED → SLICING → CLEARED_LEFT_BEHIND (momentum)} or
 * {@code ABSORBED (pitch-and-catch fade)}.
 *
 * <p>Bookmap-free: feed NQ/ES trades + depth updates in exchange time.
 * Cross-confirm uses a 2 s sync window matching research
 * {@code detector.CONFIRM_SYNC_NS}. Thresholds default to frozen V1 values.
 */
public final class SliceEventTracker {

    public enum State {
        IDLE,
        STACK_ARMED,
        SLICING,
        CLEARED_LEFT_BEHIND,
        ABSORBED
    }

    public enum SignalKind {
        MOMENTUM,
        PITCH_CATCH
    }

    public static final class Config {
        /** Pre-ignition with-trend floor (pts). */
        public double trendMinPts = 0.0;
        /** Dominant level / inferred median min. */
        public double domVsMedMin = 4.0;
        /** Soft momentum pullback-ratio warn (not a hard gate in V1). */
        public double momPullbackRatioWarn = 0.55;
        public double pcPullbackPtsMin = 4.0;
        public double pcPullbackRatioMin = 0.70;
        public double pcSlowRatioMax = 0.85;
        public double pcNetPtsMax = 1.0;
        public double stopBufferPts = 2.0;
        public int zoneTicks = 40;
        public long syncNs = 2_000_000_000L;
        public long burstWinNs = 2_000_000_000L;
        public long stallWinNs = 5_000_000_000L;
        public long stallMaxNs = 10_000_000_000L;
        public long trendLookbackNs = 300_000_000_000L;
        public long trendExcludeNs = 30_000_000_000L;
        public long cooldownNs = 30_000_000_000L;
        // Tape burst floors (research InstCfg, price in points)
        public double nqMinDispPts = 6.0;   // 24 ticks * 0.25
        public double esMinDispPts = 1.5;   // 6 ticks * 0.25
        public double nqMinVol = 250.0;
        public double esMinVol = 800.0;
        public double nqMinAbsDelta = 100.0;
        public double esMinAbsDelta = 250.0;
        public double nqMinImb = 0.40;
        public double esMinImb = 0.30;
        public boolean enableMomentum = true;
        public boolean enablePitchCatch = true;
        public boolean enableLongs = true;
        public boolean enableShorts = true;
    }

    public static final class Signal {
        public final SignalKind kind;
        public final int side;
        public final long signalTsNs;
        public final double entryRefPx;
        public final double dominantLevelPx;
        public final double stopPx;
        public final double pitchExtremePx;
        public final double trend5mPts;
        public final double domVsMed;
        public final double burstTps;
        public final double pullbackRatio;
        public final double postSlowRatio;
        public final double stallDurS;
        public final State state;
        public final long ignitionNs;
        public final long confirmNs;

        Signal(SignalKind kind, int side, long signalTsNs, double entryRefPx,
               double dominantLevelPx, double stopPx, double pitchExtremePx,
               double trend5mPts, double domVsMed, double burstTps,
               double pullbackRatio, double postSlowRatio, double stallDurS,
               State state, long ignitionNs, long confirmNs) {
            this.kind = kind;
            this.side = side;
            this.signalTsNs = signalTsNs;
            this.entryRefPx = entryRefPx;
            this.dominantLevelPx = dominantLevelPx;
            this.stopPx = stopPx;
            this.pitchExtremePx = pitchExtremePx;
            this.trend5mPts = trend5mPts;
            this.domVsMed = domVsMed;
            this.burstTps = burstTps;
            this.pullbackRatio = pullbackRatio;
            this.postSlowRatio = postSlowRatio;
            this.stallDurS = stallDurS;
            this.state = state;
            this.ignitionNs = ignitionNs;
            this.confirmNs = confirmNs;
        }

        public String toCsvRow() {
            return String.format(Locale.US,
                    "%s,%d,%d,%.4f,%.4f,%.4f,%.4f,%.3f,%.3f,%.1f,%.3f,%.3f,%.2f,%s,%d,%d",
                    kind.name(), side, signalTsNs, entryRefPx, dominantLevelPx, stopPx,
                    pitchExtremePx, trend5mPts, domVsMed, burstTps, pullbackRatio,
                    postSlowRatio, stallDurS, state.name(), ignitionNs, confirmNs);
        }

        public static String csvHeader() {
            return "kind,side,signal_ts_ns,entry_ref_px,dominant_level_px,stop_px,"
                    + "pitch_extreme_px,trend_5m_pts,dom_vs_med,burst_tps,pullback_ratio,"
                    + "post_slow_ratio,stall_dur_s,state,ignition_ns,confirm_ns";
        }
    }

    public interface Listener {
        void onSignal(Signal signal);

        void onState(State state, long tsNs);
    }

    private static final class Print {
        final long tsNs;
        final double px;
        final double sz;
        final boolean buy;

        Print(long tsNs, double px, double sz, boolean buy) {
            this.tsNs = tsNs;
            this.px = px;
            this.sz = sz;
            this.buy = buy;
        }
    }

    private static final class LegBurst {
        final int side;
        final long ignitionNs;
        final long confirmNs;
        final double prePx;
        final double endPx;
        final double vol;
        final double delta;
        final int nTrades;

        LegBurst(int side, long ignitionNs, long confirmNs, double prePx, double endPx,
                 double vol, double delta, int nTrades) {
            this.side = side;
            this.ignitionNs = ignitionNs;
            this.confirmNs = confirmNs;
            this.prePx = prePx;
            this.endPx = endPx;
            this.vol = vol;
            this.delta = delta;
            this.nTrades = nTrades;
        }
    }

    private static final class Episode {
        int side;
        long ignitionNs;
        long confirmNs;
        double nqPrePx;
        double esPrePx;
        double nqEndPx;
        int nqBurstTrades;
        double dominantLevelPx = Double.NaN;
        double domVsMed = Double.NaN;
        double trend5mPts = Double.NaN;
        double peakPastPts;
        double pullbackPts;
        double pitchExtremePx = Double.NaN;
        State state = State.STACK_ARMED;
        boolean signalled;
        long stallStartNs = -1;
    }

    private final Config cfg;
    private final Listener listener;

    private final TreeMap<Integer, Integer> nqBids = new TreeMap<>(Comparator.reverseOrder());
    private final TreeMap<Integer, Integer> nqAsks = new TreeMap<>();
    private final TreeMap<Integer, Integer> esBids = new TreeMap<>(Comparator.reverseOrder());
    private final TreeMap<Integer, Integer> esAsks = new TreeMap<>();

    private final Deque<Print> nqPrints = new ArrayDeque<>();
    private final Deque<Print> esPrints = new ArrayDeque<>();

    private LegBurst lastNqBurst;
    private LegBurst lastEsBurst;
    private Episode episode;
    private long lastSignalNs = -1;
    private State publishedState = State.IDLE;

    private double nqLastPx = Double.NaN;
    private double esLastPx = Double.NaN;
    private double tickSize = 0.25;

    public SliceEventTracker(Config cfg, Listener listener) {
        this.cfg = cfg == null ? new Config() : cfg;
        this.listener = listener;
    }

    public void setTickSize(double tickSize) {
        if (tickSize > 0) {
            this.tickSize = tickSize;
        }
    }

    public State state() {
        return episode == null ? State.IDLE : episode.state;
    }

    public EpisodeSnapshot snapshot() {
        if (episode == null) {
            return EpisodeSnapshot.idle();
        }
        double burstTps = episode.nqBurstTrades / 2.0;
        double pbRatio = episode.peakPastPts > 0 ? episode.pullbackPts / episode.peakPastPts : Double.NaN;
        return new EpisodeSnapshot(
                episode.state, episode.side, episode.dominantLevelPx, episode.domVsMed,
                episode.trend5mPts, burstTps, pbRatio, episode.peakPastPts, episode.pullbackPts,
                episode.pitchExtremePx);
    }

    public static final class EpisodeSnapshot {
        public final State state;
        public final int side;
        public final double dominantLevelPx;
        public final double domVsMed;
        public final double trend5mPts;
        public final double burstTps;
        public final double pullbackRatio;
        public final double peakPastPts;
        public final double pullbackPts;
        public final double pitchExtremePx;

        EpisodeSnapshot(State state, int side, double dominantLevelPx, double domVsMed,
                        double trend5mPts, double burstTps, double pullbackRatio,
                        double peakPastPts, double pullbackPts, double pitchExtremePx) {
            this.state = state;
            this.side = side;
            this.dominantLevelPx = dominantLevelPx;
            this.domVsMed = domVsMed;
            this.trend5mPts = trend5mPts;
            this.burstTps = burstTps;
            this.pullbackRatio = pullbackRatio;
            this.peakPastPts = peakPastPts;
            this.pullbackPts = pullbackPts;
            this.pitchExtremePx = pitchExtremePx;
        }

        public static EpisodeSnapshot idle() {
            return new EpisodeSnapshot(State.IDLE, 0, Double.NaN, Double.NaN, Double.NaN,
                    Double.NaN, Double.NaN, Double.NaN, Double.NaN, Double.NaN);
        }
    }

    public void onDepth(String role, boolean isBid, int priceTicks, int size) {
        TreeMap<Integer, Integer> book = book(role, isBid);
        if (book == null) {
            return;
        }
        if (size <= 0) {
            book.remove(priceTicks);
        } else {
            book.put(priceTicks, size);
        }
    }

    public void onNqTrade(long tsNs, double pricePts, double size, boolean buyAggressor) {
        onTrade("NQ", tsNs, pricePts, size, buyAggressor);
    }

    public void onEsTrade(long tsNs, double pricePts, double size, boolean buyAggressor) {
        onTrade("ES", tsNs, pricePts, size, buyAggressor);
    }

    public void onTime(long tsNs) {
        if (episode != null && !episode.signalled) {
            maybeResolve(tsNs);
        }
    }

    private void onTrade(String role, long tsNs, double pricePts, double size, boolean buy) {
        Deque<Print> q = "NQ".equals(role) ? nqPrints : esPrints;
        if ("NQ".equals(role)) {
            nqLastPx = pricePts;
        } else {
            esLastPx = pricePts;
        }
        q.addLast(new Print(tsNs, pricePts, size, buy));
        trim(q, tsNs - cfg.trendLookbackNs - cfg.burstWinNs);
        LegBurst burst = detectBurst(role, q, tsNs);
        if (burst != null) {
            if ("NQ".equals(role)) {
                lastNqBurst = burst;
            } else {
                lastEsBurst = burst;
            }
            tryArm(tsNs);
        }
        if (episode != null && "NQ".equals(role)) {
            updateEpisodePath(tsNs, pricePts);
            maybeResolve(tsNs);
        }
    }

    private void tryArm(long tsNs) {
        if (episode != null && !episode.signalled) {
            return;
        }
        if (lastSignalNs > 0 && tsNs - lastSignalNs < cfg.cooldownNs) {
            return;
        }
        if (lastNqBurst == null || lastEsBurst == null) {
            return;
        }
        if (lastNqBurst.side != lastEsBurst.side) {
            return;
        }
        long lag = Math.abs(lastNqBurst.ignitionNs - lastEsBurst.ignitionNs);
        if (lag > cfg.syncNs) {
            return;
        }
        int side = lastNqBurst.side;
        if (side > 0 && !cfg.enableLongs) {
            return;
        }
        if (side < 0 && !cfg.enableShorts) {
            return;
        }
        // Dominant level from NQ book at (approx) pre-burst
        DomLevel dom = findDominant(side, lastNqBurst.prePx);
        double trend = trend5m(nqPrints, side, lastNqBurst.ignitionNs);
        Episode ep = new Episode();
        ep.side = side;
        ep.ignitionNs = Math.min(lastNqBurst.ignitionNs, lastEsBurst.ignitionNs);
        ep.confirmNs = Math.max(lastNqBurst.confirmNs, lastEsBurst.confirmNs);
        ep.nqPrePx = lastNqBurst.prePx;
        ep.esPrePx = lastEsBurst.prePx;
        ep.nqEndPx = lastNqBurst.endPx;
        ep.nqBurstTrades = lastNqBurst.nTrades;
        ep.trend5mPts = trend;
        if (dom != null) {
            ep.dominantLevelPx = dom.pricePts;
            ep.domVsMed = dom.vsMed;
        }
        ep.state = State.STACK_ARMED;
        if (!Double.isNaN(ep.domVsMed) && ep.domVsMed >= 0) {
            // Move to SLICING once cross-confirmed
            ep.state = State.SLICING;
        }
        episode = ep;
        publishState(ep.state, tsNs);
    }

    private void updateEpisodePath(long tsNs, double px) {
        Episode ep = episode;
        if (ep == null || ep.signalled) {
            return;
        }
        if (Double.isNaN(ep.dominantLevelPx)) {
            return;
        }
        double past = ep.side * (px - ep.dominantLevelPx);
        if (past > ep.peakPastPts) {
            ep.peakPastPts = past;
            ep.pitchExtremePx = px;
        }
        if (ep.peakPastPts > 0) {
            double fromPeak = ep.side * (ep.pitchExtremePx - px);
            if (fromPeak > ep.pullbackPts) {
                ep.pullbackPts = fromPeak;
            }
        }
    }

    private void maybeResolve(long tsNs) {
        Episode ep = episode;
        if (ep == null || ep.signalled) {
            return;
        }
        if (tsNs < ep.confirmNs) {
            return;
        }
        long sinceConfirm = tsNs - ep.confirmNs;
        if (sinceConfirm > cfg.stallMaxNs && ep.state == State.SLICING) {
            // Timed out without signal
            clearEpisode(State.IDLE, tsNs);
            return;
        }

        boolean leftBehind = ep.peakPastPts > 0 && ep.pullbackPts >= 0
                && (ep.pullbackPts / Math.max(ep.peakPastPts, 1e-9)) <= 1.0
                && ep.side * (nqLastPx - ep.dominantLevelPx) > 0;

        // Momentum: left behind + with-trend + dominant
        if (cfg.enableMomentum
                && leftBehind
                && ep.peakPastPts > 0
                && !Double.isNaN(ep.domVsMed) && ep.domVsMed >= cfg.domVsMedMin
                && !Double.isNaN(ep.trend5mPts) && ep.trend5mPts >= cfg.trendMinPts
                && sinceConfirm >= 0) {
            // Require price still past level (left behind)
            if (ep.side * (nqLastPx - ep.dominantLevelPx) > tickSize) {
                emitMomentum(ep, tsNs);
                return;
            }
        }

        // Pitch-and-catch stall over [confirm, confirm+5..10s]
        if (cfg.enablePitchCatch && sinceConfirm >= cfg.stallWinNs) {
            StallMetrics sm = stallMetrics(ep, tsNs);
            if (sm != null
                    && sm.leftBehind == 0
                    && sm.pullbackPts >= cfg.pcPullbackPtsMin
                    && sm.pullbackRatio >= cfg.pcPullbackRatioMin
                    && sm.slowRatio <= cfg.pcSlowRatioMax
                    && sm.netPts <= cfg.pcNetPtsMax) {
                emitPitchCatch(ep, tsNs, sm);
            }
        }
    }

    private void emitMomentum(Episode ep, long tsNs) {
        ep.state = State.CLEARED_LEFT_BEHIND;
        ep.signalled = true;
        lastSignalNs = tsNs;
        publishState(ep.state, tsNs);
        double entry = nqLastPx;
        double stop = ep.dominantLevelPx - ep.side * cfg.stopBufferPts;
        double pbRatio = ep.peakPastPts > 0 ? ep.pullbackPts / ep.peakPastPts : Double.NaN;
        Signal sig = new Signal(
                SignalKind.MOMENTUM, ep.side, tsNs, entry, ep.dominantLevelPx, stop,
                ep.pitchExtremePx, ep.trend5mPts, ep.domVsMed, ep.nqBurstTrades / 2.0,
                pbRatio, Double.NaN, 0.0, ep.state, ep.ignitionNs, ep.confirmNs);
        if (listener != null) {
            listener.onSignal(sig);
        }
        episode = null;
        publishState(State.IDLE, tsNs);
    }

    private void emitPitchCatch(Episode ep, long tsNs, StallMetrics sm) {
        ep.state = State.ABSORBED;
        ep.signalled = true;
        lastSignalNs = tsNs;
        publishState(ep.state, tsNs);
        int fadeSide = -ep.side;
        double entry = nqLastPx;
        double pitch = Double.isNaN(ep.pitchExtremePx) ? entry : ep.pitchExtremePx;
        double stop = pitch - fadeSide * cfg.stopBufferPts; // beyond pitch against fade
        // For short fade (fadeSide=-1) after long push: stop above pitch → pitch - (-1)*buf = pitch+buf
        Signal sig = new Signal(
                SignalKind.PITCH_CATCH, fadeSide, tsNs, entry, ep.dominantLevelPx, stop,
                pitch, ep.trend5mPts, ep.domVsMed, ep.nqBurstTrades / 2.0,
                sm.pullbackRatio, sm.slowRatio, sm.stallDurS, ep.state, ep.ignitionNs, ep.confirmNs);
        if (listener != null) {
            listener.onSignal(sig);
        }
        episode = null;
        publishState(State.IDLE, tsNs);
    }

    private void clearEpisode(State st, long tsNs) {
        episode = null;
        publishState(st, tsNs);
    }

    private void publishState(State st, long tsNs) {
        if (st != publishedState) {
            publishedState = st;
            if (listener != null) {
                listener.onState(st, tsNs);
            }
        }
    }

    private static final class StallMetrics {
        final int leftBehind;
        final double pullbackPts;
        final double pullbackRatio;
        final double slowRatio;
        final double netPts;
        final double stallDurS;

        StallMetrics(int leftBehind, double pullbackPts, double pullbackRatio,
                     double slowRatio, double netPts, double stallDurS) {
            this.leftBehind = leftBehind;
            this.pullbackPts = pullbackPts;
            this.pullbackRatio = pullbackRatio;
            this.slowRatio = slowRatio;
            this.netPts = netPts;
            this.stallDurS = stallDurS;
        }
    }

    private StallMetrics stallMetrics(Episode ep, long tsNs) {
        long t0 = ep.confirmNs;
        long t1 = Math.min(tsNs, ep.confirmNs + cfg.stallWinNs);
        int postTrades = 0;
        Double first = null;
        Double last = null;
        for (Print p : nqPrints) {
            if (p.tsNs < t0) {
                continue;
            }
            if (p.tsNs > t1) {
                break;
            }
            postTrades++;
            if (first == null) {
                first = p.px;
            }
            last = p.px;
        }
        double burstTps = ep.nqBurstTrades / 2.0;
        double postTps = postTrades / Math.max(1e-9, (t1 - t0) / 1e9);
        double slow = burstTps > 0 ? postTps / burstTps : Double.NaN;
        double net = (first != null && last != null) ? ep.side * (last - first) : Double.NaN;
        double pbRatio = ep.peakPastPts > 0 ? ep.pullbackPts / ep.peakPastPts : Double.NaN;
        int lb = (ep.peakPastPts > tickSize && ep.side * (nqLastPx - ep.dominantLevelPx) > tickSize) ? 1 : 0;
        double stallS = (tsNs - ep.confirmNs) / 1e9;
        return new StallMetrics(lb, ep.pullbackPts, pbRatio, slow, net, stallS);
    }

    private static final class DomLevel {
        final double pricePts;
        final double vsMed;

        DomLevel(double pricePts, double vsMed) {
            this.pricePts = pricePts;
            this.vsMed = vsMed;
        }
    }

    private DomLevel findDominant(int side, double prePx) {
        TreeMap<Integer, Integer> book = side > 0 ? nqAsks : nqBids;
        if (book.isEmpty()) {
            return null;
        }
        int preTicks = (int) Math.round(prePx / tickSize);
        int lo = side > 0 ? preTicks : preTicks - cfg.zoneTicks;
        int hi = side > 0 ? preTicks + cfg.zoneTicks : preTicks;
        int bestPx = 0;
        int bestSz = 0;
        long sum = 0;
        int n = 0;
        for (var e : book.entrySet()) {
            int px = e.getKey();
            if (px < lo || px > hi) {
                continue;
            }
            int sz = e.getValue();
            sum += sz;
            n++;
            if (sz > bestSz) {
                bestSz = sz;
                bestPx = px;
            }
        }
        if (bestSz <= 0 || n == 0) {
            return null;
        }
        double med = (double) sum / n;
        double vs = med > 0 ? bestSz / med : Double.NaN;
        return new DomLevel(bestPx * tickSize, vs);
    }

    private double trend5m(Deque<Print> q, int side, long ignitionNs) {
        long tEnd = ignitionNs - cfg.trendExcludeNs;
        long tStart = ignitionNs - cfg.trendLookbackNs;
        Double p0 = null;
        Double p1 = null;
        for (Print p : q) {
            if (p.tsNs < tStart) {
                continue;
            }
            if (p.tsNs > tEnd) {
                break;
            }
            if (p0 == null) {
                p0 = p.px;
            }
            p1 = p.px;
        }
        if (p0 == null || p1 == null) {
            return Double.NaN;
        }
        return side * (p1 - p0);
    }

    private LegBurst detectBurst(String role, Deque<Print> q, long nowNs) {
        if (q.isEmpty()) {
            return null;
        }
        long winStart = nowNs - cfg.burstWinNs;
        double firstPx = Double.NaN;
        double lastPx = Double.NaN;
        double vol = 0;
        double delta = 0;
        int n = 0;
        long firstTs = -1;
        for (Print p : q) {
            if (p.tsNs < winStart) {
                continue;
            }
            if (Double.isNaN(firstPx)) {
                firstPx = p.px;
                firstTs = p.tsNs;
            }
            lastPx = p.px;
            vol += p.sz;
            delta += p.buy ? p.sz : -p.sz;
            n++;
        }
        if (n < 2 || Double.isNaN(firstPx)) {
            return null;
        }
        double disp = lastPx - firstPx;
        int side = disp > 0 ? 1 : (disp < 0 ? -1 : 0);
        if (side == 0) {
            return null;
        }
        boolean nq = "NQ".equals(role);
        double minDisp = nq ? cfg.nqMinDispPts : cfg.esMinDispPts;
        double minVol = nq ? cfg.nqMinVol : cfg.esMinVol;
        double minAbsDelta = nq ? cfg.nqMinAbsDelta : cfg.esMinAbsDelta;
        double minImb = nq ? cfg.nqMinImb : cfg.esMinImb;
        double imb = vol > 0 ? delta / vol : 0;
        if (Math.abs(disp) < minDisp || vol < minVol || Math.abs(delta) < minAbsDelta
                || Math.abs(imb) < minImb || Math.signum(delta) != side) {
            return null;
        }
        // Avoid re-firing identical bursts every trade: require new confirm end
        LegBurst prev = nq ? lastNqBurst : lastEsBurst;
        if (prev != null && nowNs - prev.confirmNs < 250_000_000L && prev.side == side) {
            return null;
        }
        return new LegBurst(side, firstTs, nowNs, firstPx, lastPx, vol, delta, n);
    }

    private TreeMap<Integer, Integer> book(String role, boolean isBid) {
        if ("NQ".equals(role)) {
            return isBid ? nqBids : nqAsks;
        }
        if ("ES".equals(role)) {
            return isBid ? esBids : esAsks;
        }
        return null;
    }

    private static void trim(Deque<Print> q, long minTs) {
        while (!q.isEmpty() && q.peekFirst().tsNs < minTs) {
            q.removeFirst();
        }
    }
}
