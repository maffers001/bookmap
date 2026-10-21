package com.bookmap.orderflow.core;

import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

/**
 * Stop-run fingerprint detector (EXP-0041 / H32).
 *
 * <p>Infers stop runs: pierce a causal reference level by {@code pierceTicks},
 * then a same-side aggression window meeting score components (vol, imbalance,
 * sweeps, distinct aggressors). Optional MBO far-side pull and ES burst.
 *
 * <p>Distances are in <b>ticks</b> (API price units). Matches
 * {@code research/features/stop_run.py} frozen defaults.
 */
public final class StopRunTracker {

    public enum Side {
        NONE,
        LONG,  // through HIGH
        SHORT  // through LOW
    }

    public static final class Signal {
        public final Side side;
        public final int levelTick;
        public final String levelKind;
        public final int triggerTick;
        public final long eventTsNs;
        public final int score;
        public final double sideVol;
        public final double imbalance;
        public final int nPrints;
        public final int nAggressors;
        public final int nSweeps;
        public final double maxPenetrationTicks;
        public final double pullRatio;
        public final boolean mboPull;
        public final boolean esConfirmed;
        public final double esLagMs;

        public Signal(
                Side side,
                int levelTick,
                String levelKind,
                int triggerTick,
                long eventTsNs,
                int score,
                double sideVol,
                double imbalance,
                int nPrints,
                int nAggressors,
                int nSweeps,
                double maxPenetrationTicks,
                double pullRatio,
                boolean mboPull,
                boolean esConfirmed,
                double esLagMs) {
            this.side = side;
            this.levelTick = levelTick;
            this.levelKind = levelKind;
            this.triggerTick = triggerTick;
            this.eventTsNs = eventTsNs;
            this.score = score;
            this.sideVol = sideVol;
            this.imbalance = imbalance;
            this.nPrints = nPrints;
            this.nAggressors = nAggressors;
            this.nSweeps = nSweeps;
            this.maxPenetrationTicks = maxPenetrationTicks;
            this.pullRatio = pullRatio;
            this.mboPull = mboPull;
            this.esConfirmed = esConfirmed;
            this.esLagMs = esLagMs;
        }
    }

    public static final class Resolve {
        public final Side side;
        public final int levelTick;
        public final String outcome; // stall | continue
        public final long resolveTsNs;

        public Resolve(Side side, int levelTick, String outcome, long resolveTsNs) {
            this.side = side;
            this.levelTick = levelTick;
            this.outcome = outcome;
            this.resolveTsNs = resolveTsNs;
        }
    }

    private static final class RefLevel {
        final String kind;
        final int priceTick;
        final long liveTsNs;
        long deadTsNs = Long.MAX_VALUE;
        boolean pierced;

        RefLevel(String kind, int priceTick, long liveTsNs) {
            this.kind = kind;
            this.priceTick = priceTick;
            this.liveTsNs = liveTsNs;
        }

        boolean isHigh() {
            return kind.endsWith("HIGH");
        }
    }

    private static final class Window {
        final Side side;
        final int levelTick;
        final String levelKind;
        final long pierceTs;
        final long endTs;
        double buyVol;
        double sellVol;
        int nPrints;
        int lastTick;
        double maxPen;
        final Map<String, AggAcc> aids = new HashMap<>();

        Window(Side side, int levelTick, String levelKind, long pierceTs, long endTs, int pierceTick) {
            this.side = side;
            this.levelTick = levelTick;
            this.levelKind = levelKind;
            this.pierceTs = pierceTs;
            this.endTs = endTs;
            this.lastTick = pierceTick;
        }
    }

    private static final class AggAcc {
        int n;
        double vol;
        int minTick;
        int maxTick;

        AggAcc(int tick, double sz) {
            n = 1;
            vol = sz;
            minTick = maxTick = tick;
        }

        void add(int tick, double sz) {
            n++;
            vol += sz;
            if (tick < minTick) {
                minTick = tick;
            }
            if (tick > maxTick) {
                maxTick = tick;
            }
        }
    }

    private static final class PendingResolve {
        final Side side;
        final int levelTick;
        final long deadlineNs;
        final long eventTsNs;

        PendingResolve(Side side, int levelTick, long eventTsNs, long deadlineNs) {
            this.side = side;
            this.levelTick = levelTick;
            this.eventTsNs = eventTsNs;
            this.deadlineNs = deadlineNs;
        }
    }

    private static final class EsBar {
        long slot;
        double buyVol;
        double sellVol;
        int n;
        int openTick;
        int closeTick;
    }

    private static final class MboOrder {
        final boolean isBid;
        int priceTick;

        MboOrder(boolean isBid, int priceTick) {
            this.isBid = isBid;
            this.priceTick = priceTick;
        }
    }

    private final int pierceTicks;
    private final long windowNs;
    private final double minVol;
    private final int minPrints;
    private final double minImbalance;
    private final int minSweeps;
    private final int sweepMinSpanTicks;
    private final int sweepMinPrints;
    private final int minAggressors;
    private final int minScore;
    private final int mboRadiusTicks;
    private final long mboPreNs;
    private final double mboMinPullRatio;
    private final long syncNs;
    private final long cooldownNs;
    private final long reclaimNs;
    private final long lookbackNs;
    private final int pullawayTicks;
    private final int invalidateBeyondTicks;
    private final boolean useSwing;
    private final boolean useSession;
    private final boolean useOvernight;
    private final LocalTime rthStartUk;
    private final ZoneId ukZone = ZoneId.of("Europe/London");

    private final List<RefLevel> levels = new ArrayList<>();
    private final List<Window> activeWins = new ArrayList<>();
    private final List<PendingResolve> pendingResolves = new ArrayList<>();
    private final Deque<Integer> lookbackTicks = new ArrayDeque<>();
    private final Deque<Long> lookbackTs = new ArrayDeque<>();

    private Integer pendingHighTick;
    private Long pendingHighTs;
    private Integer pendingLowTick;
    private Long pendingLowTs;

    private Integer runHi;
    private Integer runLo;
    private Integer lockedSessHi;
    private Long lockedSessHiTs;
    private Integer lockedSessLo;
    private Long lockedSessLoTs;

    private Integer overnightHi;
    private Integer overnightLo;
    private boolean overnightEmitted;
    private Long rthStartNs;

    private final Map<String, MboOrder> mboActive = new HashMap<>();
    private final Deque<long[]> mboEvents = new ArrayDeque<>(); // [ts, cancel?1:0, bid?1:0, px]

    private final Deque<EsBar> esBars = new ArrayDeque<>();
    private EsBar esCur;
    private static final long ES_BAR_NS = 100_000_000L; // 100ms

    private long lastFireLongNs = Long.MIN_VALUE / 4;
    private long lastFireShortNs = Long.MIN_VALUE / 4;
    private Signal pendingSignal;
    private Resolve pendingResolve;
    private int activeLevelHighTick = Integer.MIN_VALUE;
    private int activeLevelLowTick = Integer.MAX_VALUE;
    private int lastScore;

    public StopRunTracker(
            int pierceTicks,
            int windowMs,
            double minVol,
            int minPrints,
            double minImbalance,
            int minSweeps,
            int sweepMinSpanTicks,
            int sweepMinPrints,
            int minAggressors,
            int minScore,
            int mboRadiusTicks,
            int mboPreMs,
            double mboMinPullRatio,
            int syncMs,
            int cooldownMs,
            double reclaimSec,
            double lookbackSec,
            double pullawayPts,
            double invalidateBeyondPts,
            double ptsPerTick,
            boolean useSwing,
            boolean useSession,
            boolean useOvernight,
            LocalTime rthStartUk) {
        this.pierceTicks = Math.max(1, pierceTicks);
        this.windowNs = Math.max(1, windowMs) * 1_000_000L;
        this.minVol = Math.max(1.0, minVol);
        this.minPrints = Math.max(1, minPrints);
        this.minImbalance = Math.max(0.0, minImbalance);
        this.minSweeps = Math.max(0, minSweeps);
        this.sweepMinSpanTicks = Math.max(1, sweepMinSpanTicks);
        this.sweepMinPrints = Math.max(2, sweepMinPrints);
        this.minAggressors = Math.max(1, minAggressors);
        this.minScore = Math.max(1, minScore);
        this.mboRadiusTicks = Math.max(1, mboRadiusTicks);
        this.mboPreNs = Math.max(1, mboPreMs) * 1_000_000L;
        this.mboMinPullRatio = mboMinPullRatio;
        this.syncNs = Math.max(1, syncMs) * 1_000_000L;
        this.cooldownNs = Math.max(0, cooldownMs) * 1_000_000L;
        this.reclaimNs = (long) (Math.max(0.1, reclaimSec) * 1_000_000_000L);
        this.lookbackNs = (long) (Math.max(1.0, lookbackSec) * 1_000_000_000L);
        this.pullawayTicks = Math.max(1, (int) Math.round(pullawayPts / Math.max(1e-9, ptsPerTick)));
        this.invalidateBeyondTicks = Math.max(1, (int) Math.round(invalidateBeyondPts / Math.max(1e-9, ptsPerTick)));
        this.useSwing = useSwing;
        this.useSession = useSession;
        this.useOvernight = useOvernight;
        this.rthStartUk = rthStartUk != null ? rthStartUk : LocalTime.of(14, 30);
    }

    public void onTrade(long tNs, int tick, int size, boolean buyAggressor, String aggressorId) {
        if (size <= 0) {
            return;
        }
        updateRthAnchor(tNs);
        updateLookback(tNs, tick);
        updateSwingCandidates(tNs, tick);
        updateSessionLevels(tNs, tick);
        updateOvernight(tNs, tick);
        invalidateLevels(tNs, tick);
        closeWindows(tNs, tick, size, buyAggressor, aggressorId, true);
        tryPierce(tNs, tick);
        // Accumulate into open windows
        for (Window w : activeWins) {
            if (tNs <= w.endTs) {
                accumulate(w, tick, size, buyAggressor, aggressorId);
            }
        }
        updateResolves(tNs, tick);
        refreshActiveLevelDisplay(tNs);
    }

    /** Feed ES tape for same-side burst confirm. */
    public void onEsTrade(long tNs, int tick, int size, boolean buyAggressor) {
        if (size <= 0) {
            return;
        }
        long slot = (tNs / ES_BAR_NS) * ES_BAR_NS;
        if (esCur == null || esCur.slot != slot) {
            if (esCur != null) {
                esBars.addLast(esCur);
                while (!esBars.isEmpty() && tNs - (esBars.peekFirst().slot + ES_BAR_NS) > syncNs * 2) {
                    esBars.pollFirst();
                }
            }
            esCur = new EsBar();
            esCur.slot = slot;
            esCur.openTick = tick;
            esCur.closeTick = tick;
        }
        if (buyAggressor) {
            esCur.buyVol += size;
        } else {
            esCur.sellVol += size;
        }
        esCur.n++;
        esCur.closeTick = tick;
    }

    public void onMboSend(long tNs, String orderId, boolean isBid, int priceTick, int size) {
        if (orderId == null || orderId.isEmpty()) {
            return;
        }
        mboActive.put(orderId, new MboOrder(isBid, priceTick));
        mboEvents.addLast(new long[] {tNs, 0, isBid ? 1 : 0, priceTick});
        pruneMbo(tNs);
    }

    public void onMboReplace(long tNs, String orderId, int priceTick, int size) {
        MboOrder o = mboActive.get(orderId);
        if (o == null) {
            return;
        }
        o.priceTick = priceTick;
        mboEvents.addLast(new long[] {tNs, 0, o.isBid ? 1 : 0, priceTick});
        pruneMbo(tNs);
    }

    public void onMboCancel(long tNs, String orderId) {
        MboOrder o = mboActive.remove(orderId);
        if (o == null) {
            return;
        }
        mboEvents.addLast(new long[] {tNs, 1, o.isBid ? 1 : 0, o.priceTick});
        pruneMbo(tNs);
    }

    public Signal pollSignal() {
        Signal s = pendingSignal;
        pendingSignal = null;
        return s;
    }

    public Resolve pollResolve() {
        Resolve r = pendingResolve;
        pendingResolve = null;
        return r;
    }

    public int activeLevelHighTick() {
        return activeLevelHighTick == Integer.MIN_VALUE ? 0 : activeLevelHighTick;
    }

    public int activeLevelLowTick() {
        return activeLevelLowTick == Integer.MAX_VALUE ? 0 : activeLevelLowTick;
    }

    public boolean hasActiveHigh() {
        return activeLevelHighTick != Integer.MIN_VALUE;
    }

    public boolean hasActiveLow() {
        return activeLevelLowTick != Integer.MAX_VALUE;
    }

    public int lastScore() {
        return lastScore;
    }

    private void updateRthAnchor(long tNs) {
        if (rthStartNs != null) {
            return;
        }
        ZonedDateTime zdt = Instant.ofEpochSecond(0, tNs).atZone(ukZone);
        // Bookmap ns may be absolute epoch ns
        try {
            zdt = Instant.ofEpochSecond(tNs / 1_000_000_000L, tNs % 1_000_000_000L).atZone(ukZone);
        } catch (Exception ignored) {
            return;
        }
        ZonedDateTime start = zdt.toLocalDate().atTime(rthStartUk).atZone(ukZone);
        rthStartNs = start.toInstant().getEpochSecond() * 1_000_000_000L
                + start.toInstant().getNano();
    }

    private void updateLookback(long tNs, int tick) {
        lookbackTicks.addLast(tick);
        lookbackTs.addLast(tNs);
        while (!lookbackTs.isEmpty() && tNs - lookbackTs.peekFirst() > lookbackNs) {
            lookbackTs.pollFirst();
            lookbackTicks.pollFirst();
        }
    }

    private void updateSwingCandidates(long tNs, int tick) {
        if (!useSwing || lookbackTicks.isEmpty()) {
            return;
        }
        int wmax = Integer.MIN_VALUE;
        int wmin = Integer.MAX_VALUE;
        for (int t : lookbackTicks) {
            if (t > wmax) {
                wmax = t;
            }
            if (t < wmin) {
                wmin = t;
            }
        }
        // HIGH pending
        if (pendingHighTick != null) {
            if (tick > pendingHighTick) {
                pendingHighTick = tick;
                pendingHighTs = tNs;
            } else if (pendingHighTick - tick >= pullawayTicks) {
                levels.add(new RefLevel("SWING_HIGH", pendingHighTick, tNs));
                pendingHighTick = null;
                pendingHighTs = null;
            }
        }
        if (pendingLowTick != null) {
            if (tick < pendingLowTick) {
                pendingLowTick = tick;
                pendingLowTs = tNs;
            } else if (tick - pendingLowTick >= pullawayTicks) {
                levels.add(new RefLevel("SWING_LOW", pendingLowTick, tNs));
                pendingLowTick = null;
                pendingLowTs = null;
            }
        }
        if (tick >= wmax) {
            if (pendingHighTick == null || tick > pendingHighTick) {
                pendingHighTick = tick;
                pendingHighTs = tNs;
            }
        }
        if (tick <= wmin) {
            if (pendingLowTick == null || tick < pendingLowTick) {
                pendingLowTick = tick;
                pendingLowTs = tNs;
            }
        }
    }

    private void updateSessionLevels(long tNs, int tick) {
        if (!useSession || rthStartNs == null || tNs < rthStartNs) {
            return;
        }
        if (runHi == null || tick > runHi) {
            runHi = tick;
        }
        if (runLo == null || tick < runLo) {
            runLo = tick;
        }
        if (lockedSessHi == null && runHi != null && runHi - tick >= pullawayTicks) {
            lockedSessHi = runHi;
            lockedSessHiTs = tNs;
            levels.add(new RefLevel("SESSION_HIGH", lockedSessHi, tNs));
        }
        if (lockedSessLo == null && runLo != null && tick - runLo >= pullawayTicks) {
            lockedSessLo = runLo;
            lockedSessLoTs = tNs;
            levels.add(new RefLevel("SESSION_LOW", lockedSessLo, tNs));
        }
        if (lockedSessHi != null && tick >= lockedSessHi + invalidateBeyondTicks) {
            markDead("SESSION_HIGH", lockedSessHi, tNs);
            lockedSessHi = null;
            lockedSessHiTs = null;
            runHi = tick;
        }
        if (lockedSessLo != null && tick <= lockedSessLo - invalidateBeyondTicks) {
            markDead("SESSION_LOW", lockedSessLo, tNs);
            lockedSessLo = null;
            lockedSessLoTs = null;
            runLo = tick;
        }
    }

    private void updateOvernight(long tNs, int tick) {
        if (!useOvernight || rthStartNs == null) {
            return;
        }
        if (tNs < rthStartNs) {
            if (overnightHi == null || tick > overnightHi) {
                overnightHi = tick;
            }
            if (overnightLo == null || tick < overnightLo) {
                overnightLo = tick;
            }
            return;
        }
        if (!overnightEmitted) {
            overnightEmitted = true;
            if (overnightHi != null) {
                levels.add(new RefLevel("ON_HIGH", overnightHi, rthStartNs));
            }
            if (overnightLo != null) {
                levels.add(new RefLevel("ON_LOW", overnightLo, rthStartNs));
            }
        }
    }

    private void markDead(String kind, int priceTick, long tNs) {
        for (RefLevel lv : levels) {
            if (lv.kind.equals(kind) && lv.priceTick == priceTick && lv.deadTsNs == Long.MAX_VALUE) {
                lv.deadTsNs = tNs;
            }
        }
    }

    private void invalidateLevels(long tNs, int tick) {
        for (RefLevel lv : levels) {
            if (lv.deadTsNs != Long.MAX_VALUE) {
                continue;
            }
            if (lv.isHigh() && tick >= lv.priceTick + invalidateBeyondTicks) {
                lv.deadTsNs = tNs;
            } else if (!lv.isHigh() && tick <= lv.priceTick - invalidateBeyondTicks) {
                lv.deadTsNs = tNs;
            }
        }
    }

    private void tryPierce(long tNs, int tick) {
        for (RefLevel lv : levels) {
            if (lv.pierced) {
                continue;
            }
            if (tNs < lv.liveTsNs || tNs >= lv.deadTsNs) {
                continue;
            }
            Side side = Side.NONE;
            if (lv.isHigh() && tick >= lv.priceTick + pierceTicks) {
                side = Side.LONG;
            } else if (!lv.isHigh() && tick <= lv.priceTick - pierceTicks) {
                side = Side.SHORT;
            }
            if (side == Side.NONE) {
                continue;
            }
            lv.pierced = true;
            // Pierce print is accumulated by onTrade after tryPierce returns.
            activeWins.add(new Window(side, lv.priceTick, lv.kind, tNs, tNs + windowNs, tick));
        }
    }

    private void accumulate(Window w, int tick, int size, boolean buyAggressor, String aggressorId) {
        if (size > 0) {
            if (buyAggressor) {
                w.buyVol += size;
            } else {
                w.sellVol += size;
            }
            w.nPrints++;
            if (aggressorId != null && !aggressorId.isEmpty()) {
                AggAcc a = w.aids.get(aggressorId);
                if (a == null) {
                    w.aids.put(aggressorId, new AggAcc(tick, size));
                } else {
                    a.add(tick, size);
                }
            }
        }
        w.lastTick = tick;
        if (w.side == Side.LONG) {
            w.maxPen = Math.max(w.maxPen, tick - w.levelTick);
        } else {
            w.maxPen = Math.max(w.maxPen, w.levelTick - tick);
        }
    }

    private void closeWindows(
            long tNs, int tick, int size, boolean buyAggressor, String aggressorId, boolean beforeAccumulate) {
        Iterator<Window> it = activeWins.iterator();
        while (it.hasNext()) {
            Window w = it.next();
            if (tNs < w.endTs) {
                continue;
            }
            // Window ended — evaluate on last state (prints already accumulated up to previous trades)
            maybeFire(w, tNs);
            it.remove();
        }
    }

    private void maybeFire(Window w, long nowNs) {
        double total = w.buyVol + w.sellVol;
        if (total <= 0 || w.nPrints < minPrints) {
            return;
        }
        double imb = (w.buyVol - w.sellVol) / total;
        double sideVol;
        double sideImb;
        if (w.side == Side.LONG) {
            sideVol = w.buyVol;
            sideImb = imb;
        } else {
            sideVol = w.sellVol;
            sideImb = -imb;
        }
        if (sideVol < minVol || sideImb < minImbalance) {
            return;
        }
        int nSweeps = 0;
        for (AggAcc a : w.aids.values()) {
            if (a.n >= sweepMinPrints && (a.maxTick - a.minTick) >= sweepMinSpanTicks) {
                nSweeps++;
            }
        }
        int nAggr = w.aids.size();
        int cVol = sideVol >= minVol ? 1 : 0;
        int cImb = sideImb >= minImbalance ? 1 : 0;
        int cSwp = nSweeps >= minSweeps ? 1 : 0;
        int cAgg = nAggr >= minAggressors ? 1 : 0;
        int score = cVol + cImb + cSwp + cAgg;
        if (score < minScore) {
            return;
        }
        long lastFire = w.side == Side.LONG ? lastFireLongNs : lastFireShortNs;
        if (w.pierceTs + windowNs - lastFire < cooldownNs) {
            // use event time ≈ window end
        }
        long eventTs = Math.min(nowNs, w.endTs);
        if (eventTs - lastFire < cooldownNs) {
            return;
        }
        double pull = pullRatio(w.pierceTs, w.levelTick, w.side);
        boolean mboPull = !Double.isNaN(pull) && pull >= mboMinPullRatio;
        double esLag = Double.NaN;
        boolean esConf = false;
        long esBurst = findEsBurst(eventTs, w.side);
        if (esBurst != Long.MIN_VALUE) {
            esLag = (esBurst - eventTs) / 1_000_000.0;
            esConf = true;
        }
        lastScore = score;
        if (w.side == Side.LONG) {
            lastFireLongNs = eventTs;
        } else {
            lastFireShortNs = eventTs;
        }
        pendingSignal = new Signal(
                w.side,
                w.levelTick,
                w.levelKind,
                w.lastTick,
                eventTs,
                score,
                sideVol,
                sideImb,
                w.nPrints,
                nAggr,
                nSweeps,
                w.maxPen,
                Double.isNaN(pull) ? -1.0 : pull,
                mboPull,
                esConf,
                Double.isNaN(esLag) ? 1e9 : esLag);
        pendingResolves.add(new PendingResolve(w.side, w.levelTick, eventTs, eventTs + reclaimNs));
    }

    private double pullRatio(long pierceTs, int levelTick, Side side) {
        boolean wantBid = side == Side.SHORT;
        long t0 = pierceTs - mboPreNs;
        int cancels = 0;
        int sends = 0;
        for (long[] e : mboEvents) {
            long ts = e[0];
            if (ts < t0 || ts > pierceTs) {
                continue;
            }
            boolean isCancel = e[1] == 1;
            boolean isBid = e[2] == 1;
            int px = (int) e[3];
            if (isBid != wantBid) {
                continue;
            }
            if (Math.abs(px - levelTick) > mboRadiusTicks) {
                continue;
            }
            if (isCancel) {
                cancels++;
            } else {
                sends++;
            }
        }
        int total = cancels + sends;
        if (total <= 0) {
            return Double.NaN;
        }
        return (double) cancels / total;
    }

    private long findEsBurst(long eventTs, Side side) {
        if (esCur != null) {
            esBars.addLast(esCur);
            esCur = null;
        }
        long best = Long.MIN_VALUE;
        long bestAbs = Long.MAX_VALUE;
        for (EsBar b : esBars) {
            long endTs = b.slot + ES_BAR_NS - 1;
            long lag = Math.abs(endTs - eventTs);
            if (lag > syncNs) {
                continue;
            }
            double total = b.buyVol + b.sellVol;
            if (total < 15.0 || b.n < 6) {
                continue;
            }
            double imb = (b.buyVol - b.sellVol) / total;
            int net = b.closeTick - b.openTick;
            boolean ok = side == Side.LONG
                    ? (imb >= 0.55 && net > 0)
                    : (imb <= -0.55 && net < 0);
            if (!ok) {
                continue;
            }
            if (lag < bestAbs) {
                bestAbs = lag;
                best = endTs;
            }
        }
        return best;
    }

    private void updateResolves(long tNs, int tick) {
        Iterator<PendingResolve> it = pendingResolves.iterator();
        while (it.hasNext()) {
            PendingResolve pr = it.next();
            boolean stall = false;
            if (pr.side == Side.LONG && tick <= pr.levelTick) {
                stall = true;
            } else if (pr.side == Side.SHORT && tick >= pr.levelTick) {
                stall = true;
            }
            if (stall) {
                pendingResolve = new Resolve(pr.side, pr.levelTick, "stall", tNs);
                it.remove();
            } else if (tNs >= pr.deadlineNs) {
                pendingResolve = new Resolve(pr.side, pr.levelTick, "continue", tNs);
                it.remove();
            }
        }
    }

    private void refreshActiveLevelDisplay(long tNs) {
        int hi = Integer.MIN_VALUE;
        int lo = Integer.MAX_VALUE;
        for (RefLevel lv : levels) {
            if (tNs < lv.liveTsNs || tNs >= lv.deadTsNs || lv.pierced) {
                continue;
            }
            if (lv.isHigh() && lv.priceTick > hi) {
                hi = lv.priceTick;
            }
            if (!lv.isHigh() && lv.priceTick < lo) {
                lo = lv.priceTick;
            }
        }
        activeLevelHighTick = hi;
        activeLevelLowTick = lo;
    }

    private void pruneMbo(long tNs) {
        long keep = tNs - 120_000_000_000L; // 2 minutes
        while (!mboEvents.isEmpty() && mboEvents.peekFirst()[0] < keep) {
            mboEvents.pollFirst();
        }
    }
}
