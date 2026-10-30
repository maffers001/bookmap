package com.bookmap.orderflow.lvn;

import java.nio.file.Path;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

import com.bookmap.orderflow.core.LvnLiveVap;
import com.bookmap.orderflow.core.LvnNodeDetector;
import com.bookmap.orderflow.core.LvnStackFile;
import com.bookmap.orderflow.core.LvnVapSeedFile;

import velox.api.layer1.common.Log;

/**
 * Process-wide LVN/HVN/DOP levels + IRT-style profile knobs for the display
 * module and auto-trade consumers.
 */
public final class LvnLevelsState {

    public static final class MarketKnobs {
        public int windowDays;
        public int sensitivity;
        /** UK HH:MM join / seed through. */
        public String joinUkHm = "13:55";
        public String dopOpenUkHm = "14:30";
        public int lvnAbove = 3;
        public int lvnBelow = 3;
        public int hvnAbove = 3;
        public int hvnBelow = 3;
        public int refreshMin = 5;

        public static MarketKnobs defaultsFor(String market) {
            MarketKnobs k = new MarketKnobs();
            if ("NQ".equalsIgnoreCase(market)) {
                k.windowDays = 30;
                k.sensitivity = 100;
            } else {
                k.windowDays = 90;
                k.sensitivity = 15;
            }
            return k;
        }

        public MarketKnobs copy() {
            MarketKnobs c = new MarketKnobs();
            c.windowDays = windowDays;
            c.sensitivity = sensitivity;
            c.joinUkHm = joinUkHm;
            c.dopOpenUkHm = dopOpenUkHm;
            c.lvnAbove = lvnAbove;
            c.lvnBelow = lvnBelow;
            c.hvnAbove = hvnAbove;
            c.hvnBelow = hvnBelow;
            c.refreshMin = refreshMin;
            return c;
        }
    }

    public static final class MarketLevels {
        public final String market;
        public final List<Double> lvnAbove; // nearest first
        public final List<Double> lvnBelow;
        public final List<Double> hvnAbove;
        public final List<Double> hvnBelow;
        public final List<Double> allLvns; // sorted, for confluence
        public final double dop;
        public final double mid;
        public final LocalDateTime asOf;
        public final String warning;
        public final int windowDaysUsed;
        public final int sensitivity;

        public MarketLevels(
                String market,
                List<Double> lvnAbove,
                List<Double> lvnBelow,
                List<Double> hvnAbove,
                List<Double> hvnBelow,
                List<Double> allLvns,
                double dop,
                double mid,
                LocalDateTime asOf,
                String warning,
                int windowDaysUsed,
                int sensitivity) {
            this.market = market;
            this.lvnAbove = List.copyOf(lvnAbove);
            this.lvnBelow = List.copyOf(lvnBelow);
            this.hvnAbove = List.copyOf(hvnAbove);
            this.hvnBelow = List.copyOf(hvnBelow);
            this.allLvns = List.copyOf(allLvns);
            this.dop = dop;
            this.mid = mid;
            this.asOf = asOf;
            this.warning = warning == null ? "" : warning;
            this.windowDaysUsed = windowDaysUsed;
            this.sensitivity = sensitivity;
        }

        public boolean hasLvns() {
            return !allLvns.isEmpty() || !lvnAbove.isEmpty() || !lvnBelow.isEmpty();
        }
    }

    public static final class Snapshot {
        public final MarketLevels es;
        public final MarketLevels nq;
        public final MarketKnobs esKnobs;
        public final MarketKnobs nqKnobs;
        public final boolean showLvn;
        public final boolean showHvn;
        public final boolean showDop;
        public final Path esSeedPath;
        public final Path nqSeedPath;
        public final String statusDetail;
        public final String lastEvent;
        public final LocalDate sessionDate;
        public final LvnApproachAlert.Config alert;

        Snapshot(MarketLevels es, MarketLevels nq,
                 MarketKnobs esKnobs, MarketKnobs nqKnobs,
                 boolean showLvn, boolean showHvn, boolean showDop,
                 Path esSeedPath, Path nqSeedPath,
                 String statusDetail, String lastEvent, LocalDate sessionDate,
                 LvnApproachAlert.Config alert) {
            this.es = es;
            this.nq = nq;
            this.esKnobs = esKnobs;
            this.nqKnobs = nqKnobs;
            this.showLvn = showLvn;
            this.showHvn = showHvn;
            this.showDop = showDop;
            this.esSeedPath = esSeedPath;
            this.nqSeedPath = nqSeedPath;
            this.statusDetail = statusDetail;
            this.lastEvent = lastEvent;
            this.sessionDate = sessionDate;
            this.alert = alert;
        }
    }

    private static final AtomicBoolean SHOW_LVN = new AtomicBoolean(true);
    private static final AtomicBoolean SHOW_HVN = new AtomicBoolean(true);
    private static final AtomicBoolean SHOW_DOP = new AtomicBoolean(true);

    private static final MarketKnobs ES_KNOBS = MarketKnobs.defaultsFor("ES");
    private static final MarketKnobs NQ_KNOBS = MarketKnobs.defaultsFor("NQ");

    private static final LvnLiveVap ES_VAP = new LvnLiveVap();
    private static final LvnLiveVap NQ_VAP = new LvnLiveVap();

    private static volatile Path esSeedPath;
    private static volatile Path nqSeedPath;
    private static volatile MarketLevels esLevels;
    private static volatile MarketLevels nqLevels;
    private static volatile double esDop = Double.NaN;
    private static volatile double nqDop = Double.NaN;
    private static volatile LocalDate sessionDateUk;
    private static volatile String statusDetail = "Load VAP seed (ES / NQ)";
    private static volatile String lastEvent = "";
    private static volatile LvnStackFile.Stack esStack;
    private static volatile LvnStackFile.Stack nqStack;

    private static final ConcurrentHashMap<String, Integer> ATTACH = new ConcurrentHashMap<>();
    private static final List<Consumer<Snapshot>> LISTENERS = new CopyOnWriteArrayList<>();
    private static final List<Runnable> APPLY_HOOKS = new CopyOnWriteArrayList<>();
    private static final LvnApproachAlert APPROACH_ALERT = new LvnApproachAlert();

    static {
        APPROACH_ALERT.setListener(ev -> {
            lastEvent = ev.message;
            fire();
        });
    }

    private LvnLevelsState() {}

    public static void addListener(Consumer<Snapshot> listener) {
        if (listener != null) {
            LISTENERS.add(listener);
            listener.accept(snapshot());
        }
    }

    public static void removeListener(Consumer<Snapshot> listener) {
        LISTENERS.remove(listener);
    }

    /** Modules register so Apply / seed load can force a recompute on the feed thread. */
    public static void registerApplyHook(Runnable r) {
        if (r != null) {
            APPLY_HOOKS.add(r);
        }
    }

    public static void unregisterApplyHook(Runnable r) {
        APPLY_HOOKS.remove(r);
    }

    public static void registerAttach(String role) {
        if (role == null || role.isEmpty()) {
            return;
        }
        ATTACH.merge(role, 1, Integer::sum);
        fire();
    }

    public static void unregisterAttach(String role) {
        if (role == null) {
            return;
        }
        ATTACH.computeIfPresent(role, (k, v) -> v <= 1 ? null : v - 1);
        fire();
    }

    public static MarketKnobs knobsFor(String market) {
        return "NQ".equalsIgnoreCase(market) ? NQ_KNOBS.copy() : ES_KNOBS.copy();
    }

    public static synchronized void applyKnobs(String market, MarketKnobs in, String via) {
        if (in == null) {
            return;
        }
        MarketKnobs dest = "NQ".equalsIgnoreCase(market) ? NQ_KNOBS : ES_KNOBS;
        dest.windowDays = Math.max(1, Math.min(400, in.windowDays));
        dest.sensitivity = Math.max(1, Math.min(500, in.sensitivity));
        dest.joinUkHm = in.joinUkHm == null || in.joinUkHm.isBlank() ? "13:55" : in.joinUkHm.trim();
        dest.dopOpenUkHm = in.dopOpenUkHm == null || in.dopOpenUkHm.isBlank() ? "14:30" : in.dopOpenUkHm.trim();
        int max = LvnStackFile.MAX_DISPLAY_EACH_SIDE;
        dest.lvnAbove = clamp(in.lvnAbove, 0, max);
        dest.lvnBelow = clamp(in.lvnBelow, 0, max);
        dest.hvnAbove = clamp(in.hvnAbove, 0, max);
        dest.hvnBelow = clamp(in.hvnBelow, 0, max);
        dest.refreshMin = clamp(in.refreshMin, 1, 30);
        LvnLiveVap vap = vapFor(market);
        if (vap != null && vap.hasSeed()) {
            LocalDate day = sessionDate();
            LocalTime jt = LvnLiveVap.parseUkHm(dest.joinUkHm);
            vap.setJoinThrough(LocalDateTime.of(day, jt));
        }
        Log.info(String.format(Locale.US,
                "LVN Levels knobs %s via=%s window=%dd S=%d join=%s dopOpen=%s LVN↑%d↓%d HVN↑%d↓%d refresh=%dm",
                market, via, dest.windowDays, dest.sensitivity, dest.joinUkHm, dest.dopOpenUkHm,
                dest.lvnAbove, dest.lvnBelow, dest.hvnAbove, dest.hvnBelow, dest.refreshMin));
        lastEvent = market + " knobs applied (rebuild deferred to module)";
        statusDetail = refreshStatus();
        fire();
        // Modules rebuild on Apply only after realtime — never on the Loading-events thread.
        runApplyHooks();
    }

    /** Parse the seed file off the calling (EDT) thread, then bind. */
    public static void bindSeed(String market, Path path, String via) {
        Thread t = new Thread(() -> bindSeedLoaded(market, path, via), "LvnSeedLoad-" + market);
        t.setDaemon(true);
        t.start();
    }

    private static synchronized void bindSeedLoaded(String market, Path path, String via) {
        try {
            LvnVapSeedFile.Seed seed = LvnVapSeedFile.load(path);
            String mkt = market == null ? "" : market.trim().toUpperCase(Locale.ROOT);
            if (seed.meta.market != null && !seed.meta.market.isBlank()
                    && !seed.meta.market.equalsIgnoreCase(mkt)) {
                lastEvent = "Seed rejected: file is " + seed.meta.market
                        + " seed, not " + mkt + " (" + seed.root.getFileName() + ")";
                Log.info("LVN Levels " + lastEvent);
                statusDetail = refreshStatus();
                fire();
                return;
            }
            LvnLiveVap vap = vapFor(market);
            MarketKnobs knobs = "NQ".equalsIgnoreCase(market) ? NQ_KNOBS : ES_KNOBS;
            vap.loadSeed(seed);
            // Prefer meta through; sync join knob clock on session date
            if (seed.meta.throughUk != null) {
                LocalTime t = seed.meta.throughUk.toLocalTime();
                knobs.joinUkHm = String.format(Locale.US, "%02d:%02d", t.getHour(), t.getMinute());
                LocalDate day = sessionDate();
                vap.setJoinThrough(LocalDateTime.of(day, t));
            }
            if ("NQ".equalsIgnoreCase(market)) {
                nqSeedPath = seed.root;
            } else {
                esSeedPath = seed.root;
            }
            // Do NOT detect nodes here — Bookmap "Loading events" shares this thread.
            lastEvent = market + " seed loaded " + seed.root.getFileName()
                    + " through=" + knobs.joinUkHm + " (nodes on realtime/Apply)";
            Log.info("LVN Levels seed bound " + market + " via=" + via
                    + " root=" + seed.root + " through=" + seed.meta.throughUk);
            statusDetail = refreshStatus();
            fire();
            runApplyHooks();
        } catch (Exception e) {
            Log.info("LVN Levels seed bind failed: " + e.getMessage());
            lastEvent = "Seed load failed: " + e.getMessage();
            fire();
        }
    }

    /** Optional fixed stack CSV (confluence fast-path). */
    public static synchronized void bindStack(String market, Path path, String via) {
        try {
            LvnStackFile.Stack st = LvnStackFile.load(path);
            if ("NQ".equalsIgnoreCase(market)) {
                nqStack = st;
            } else {
                esStack = st;
            }
            // Also keep on trade state for backward compat
            com.bookmap.orderflow.trade.LvnStrategyState.bindStack(market, path, via);
            lastEvent = market + " stack " + st.snapshots.size() + " snaps";
            fire();
        } catch (Exception e) {
            lastEvent = "Stack load failed: " + e.getMessage();
            fire();
        }
    }

    public static LvnStackFile.Stack stackFor(String market) {
        if ("NQ".equalsIgnoreCase(market)) {
            return nqStack;
        }
        if ("ES".equalsIgnoreCase(market)) {
            return esStack;
        }
        return null;
    }

    public static LvnLiveVap vapFor(String market) {
        if ("NQ".equalsIgnoreCase(market)) {
            return NQ_VAP;
        }
        if ("ES".equalsIgnoreCase(market)) {
            return ES_VAP;
        }
        return null;
    }

    public static MarketLevels levelsFor(String market) {
        if ("NQ".equalsIgnoreCase(market)) {
            return nqLevels;
        }
        if ("ES".equalsIgnoreCase(market)) {
            return esLevels;
        }
        return null;
    }

    /** All LVN prices for confluence (from last rebuild). */
    public static List<Double> allLvnsFor(String market) {
        MarketLevels lv = levelsFor(market);
        return lv == null ? List.of() : lv.allLvns;
    }

    public static double dopFor(String market) {
        if ("NQ".equalsIgnoreCase(market)) {
            return nqDop;
        }
        if ("ES".equalsIgnoreCase(market)) {
            return esDop;
        }
        return Double.NaN;
    }

    public static synchronized void setDop(String market, double px) {
        if ("NQ".equalsIgnoreCase(market)) {
            nqDop = px;
        } else if ("ES".equalsIgnoreCase(market)) {
            esDop = px;
        }
        // refresh published levels with new DOP
        MarketLevels cur = levelsFor(market);
        if (cur != null) {
            publishLevels(market, cur.lvnAbove, cur.lvnBelow, cur.hvnAbove, cur.hvnBelow,
                    cur.allLvns, px, cur.mid, cur.asOf, cur.warning,
                    cur.windowDaysUsed, cur.sensitivity);
        } else {
            fire();
        }
    }

    public static synchronized void clearDop(String market) {
        if ("NQ".equalsIgnoreCase(market)) {
            nqDop = Double.NaN;
        } else if ("ES".equalsIgnoreCase(market)) {
            esDop = Double.NaN;
        }
        fire();
    }

    public static boolean showLvn() {
        return SHOW_LVN.get();
    }

    public static boolean showHvn() {
        return SHOW_HVN.get();
    }

    public static boolean showDop() {
        return SHOW_DOP.get();
    }

    public static void setShowLvn(boolean on, String via) {
        SHOW_LVN.set(on);
        lastEvent = "Show LVN " + (on ? "ON" : "OFF") + " (" + via + ")";
        fire();
    }

    public static void setShowHvn(boolean on, String via) {
        SHOW_HVN.set(on);
        lastEvent = "Show HVN " + (on ? "ON" : "OFF") + " (" + via + ")";
        fire();
    }

    public static void setShowDop(boolean on, String via) {
        SHOW_DOP.set(on);
        lastEvent = "Show DOP " + (on ? "ON" : "OFF") + " (" + via + ")";
        fire();
    }

    public static LvnApproachAlert.Config alertConfig() {
        return APPROACH_ALERT.config();
    }

    public static synchronized void applyAlertConfig(LvnApproachAlert.Config in, String via) {
        APPROACH_ALERT.applyConfig(in);
        LvnApproachAlert.Config c = APPROACH_ALERT.config();
        lastEvent = String.format(Locale.US,
                "Alert %s mode=%s ES approach=%.1f pullback=%.1f | NQ approach=%.1f pullback=%.1f cool=%ds corrWin=%ds LVN=%s DOP=%s sound=%s (%s)",
                c.enabled ? "ON" : "OFF", c.mode,
                c.esApproachPts, c.esPullbackPts, c.nqApproachPts, c.nqPullbackPts,
                c.cooldownSec, c.corrWindowSec,
                c.watchLvn, c.watchDop, c.sound, via);
        fire();
    }

    /**
     * Realtime mid sample for approach alerts (call from Levels module interval).
     */
    public static void onMidUpdate(String market, double mid, long nowNs) {
        APPROACH_ALERT.onMid(market, mid, nowNs);
    }

    public static LocalDate sessionDate() {
        return sessionDateUk != null ? sessionDateUk : LocalDate.now(LvnStackFile.UK);
    }

    /**
     * Feed UK date only — must stay cheap. Never run node detection here;
     * Bookmap calls this throughout historical "Loading events".
     */
    public static synchronized void setSessionDate(LocalDate day) {
        if (day == null || day.equals(sessionDateUk)) {
            return;
        }
        LocalDate prev = sessionDateUk;
        sessionDateUk = day;
        if (prev != null) {
            esDop = Double.NaN;
            nqDop = Double.NaN;
            ES_VAP.clearLive();
            NQ_VAP.clearLive();
            APPROACH_ALERT.resetSession();
        }
        if (ES_VAP.hasSeed()) {
            ES_VAP.setJoinThrough(LocalDateTime.of(day, LvnLiveVap.parseUkHm(ES_KNOBS.joinUkHm)));
        }
        if (NQ_VAP.hasSeed()) {
            NQ_VAP.setJoinThrough(LocalDateTime.of(day, LvnLiveVap.parseUkHm(NQ_KNOBS.joinUkHm)));
        }
        lastEvent = "Session date " + day;
        statusDetail = refreshStatus();
        fire();
    }

    /**
     * Rebuild nodes from VAP for {@code market} around {@code mid} as-of {@code asOf}.
     */
    public static synchronized MarketLevels rebuild(String market, double mid, LocalDateTime asOf) {
        LvnLiveVap vap = vapFor(market);
        MarketKnobs knobs = "NQ".equalsIgnoreCase(market) ? NQ_KNOBS : ES_KNOBS;
        if (vap == null || !vap.hasSeed()) {
            return levelsFor(market);
        }
        LvnLiveVap.NodesSnapshot snap = vap.rebuild(knobs.windowDays, knobs.sensitivity, asOf);
        double ref = mid > 0 ? mid : (snap.asOf != null ? 0 : 0);
        if (!(ref > 0) && !snap.vap.isEmpty()) {
            // VPOC of vap as mid fallback
            int bestRow = 0;
            double bestVol = -1;
            for (var e : snap.vap.entrySet()) {
                if (e.getValue() > bestVol) {
                    bestVol = e.getValue();
                    bestRow = e.getKey();
                }
            }
            ref = LvnNodeDetector.rowToPrice(bestRow, vap.tick());
        }
        LvnNodeDetector.Selection lvnSel = LvnNodeDetector.selectAround(
                snap.valleys, ref, knobs.lvnAbove, knobs.lvnBelow);
        LvnNodeDetector.Selection hvnSel = LvnNodeDetector.selectAround(
                snap.peaks, ref, knobs.hvnAbove, knobs.hvnBelow);
        List<Double> allLvns = LvnNodeDetector.pricesOf(snap.valleys);
        double dop = dopFor(market);
        return publishLevels(market, lvnSel.above, lvnSel.below, hvnSel.above, hvnSel.below,
                allLvns, dop, ref, snap.asOf, snap.warning, snap.windowDaysUsed, snap.sensitivity);
    }

    private static MarketLevels publishLevels(
            String market,
            List<Double> lvnAbove,
            List<Double> lvnBelow,
            List<Double> hvnAbove,
            List<Double> hvnBelow,
            List<Double> allLvns,
            double dop,
            double mid,
            LocalDateTime asOf,
            String warning,
            int windowUsed,
            int sens) {
        MarketLevels lv = new MarketLevels(
                market, lvnAbove, lvnBelow, hvnAbove, hvnBelow, allLvns,
                dop, mid, asOf, warning, windowUsed, sens);
        if ("NQ".equalsIgnoreCase(market)) {
            nqLevels = lv;
        } else {
            esLevels = lv;
        }
        statusDetail = refreshStatus();
        lastEvent = String.format(Locale.US, "%s nodes LVN=%d HVN↑%d↓%d window=%dd S=%d",
                market, allLvns.size(), hvnAbove.size(), hvnBelow.size(), windowUsed, sens);
        fire();
        return lv;
    }

    public static Snapshot snapshot() {
        return new Snapshot(
                esLevels, nqLevels, ES_KNOBS.copy(), NQ_KNOBS.copy(),
                SHOW_LVN.get(), SHOW_HVN.get(), SHOW_DOP.get(),
                esSeedPath, nqSeedPath, statusDetail, lastEvent, sessionDate(),
                APPROACH_ALERT.config());
    }

    private static String refreshStatus() {
        StringBuilder sb = new StringBuilder();
        sb.append("Session ").append(sessionDate()).append(" | ");
        appendMkt(sb, "ES", esLevels, ES_KNOBS, esSeedPath, esDop);
        sb.append(" | ");
        appendMkt(sb, "NQ", nqLevels, NQ_KNOBS, nqSeedPath, nqDop);
        return sb.toString();
    }

    private static void appendMkt(StringBuilder sb, String mkt, MarketLevels lv,
                                  MarketKnobs k, Path seed, double dop) {
        sb.append(mkt).append(" window=").append(k.windowDays).append("d S=").append(k.sensitivity);
        sb.append(" join=").append(k.joinUkHm);
        if (seed != null) {
            sb.append(" seed=").append(seed.getFileName());
        }
        if (lv != null) {
            sb.append(String.format(Locale.US,
                    " LVN↑%d↓%d (all %d) HVN↑%d↓%d mid=%.2f nearest LVN↑%s LVN↓%s HVN↑%s",
                    lv.lvnAbove.size(), lv.lvnBelow.size(), lv.allLvns.size(),
                    lv.hvnAbove.size(), lv.hvnBelow.size(), lv.mid,
                    lv.lvnAbove.isEmpty() ? "-" : String.format(Locale.US, "%.2f", lv.lvnAbove.get(0)),
                    lv.lvnBelow.isEmpty() ? "-" : String.format(Locale.US, "%.2f", lv.lvnBelow.get(0)),
                    lv.hvnAbove.isEmpty() ? "-" : String.format(Locale.US, "%.2f", lv.hvnAbove.get(0))));
            if (lv.warning != null && !lv.warning.isEmpty()) {
                sb.append(" !").append(lv.warning);
            }
        } else if (seed != null) {
            sb.append(" (seed loaded — waiting rebuild/attach)");
        }
        if (!Double.isNaN(dop)) {
            sb.append(String.format(Locale.US, " DOP=%.2f", dop));
        }
    }

    private static void runApplyHooks() {
        for (Runnable r : APPLY_HOOKS) {
            try {
                r.run();
            } catch (Throwable t) {
                Log.info("LVN Levels apply hook failed: " + t.getMessage());
            }
        }
    }

    private static void fire() {
        Snapshot snap = snapshot();
        for (Consumer<Snapshot> listener : LISTENERS) {
            try {
                listener.accept(snap);
            } catch (RuntimeException ex) {
                Log.info("LVN Levels listener failed: " + ex.getMessage());
            }
        }
    }

    private static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }
}
