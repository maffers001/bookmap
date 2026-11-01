package com.bookmap.orderflow.lvn;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
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

    /**
     * Default IRT seed parent dir ({@code vap_seed_ES} / {@code vap_seed_NQ}).
     * Override with env {@code LVN_SEED_ROOT}.
     */
    public static final Path DEFAULT_SEED_ROOT = defaultSeedRoot();
    /** Default Join / seed --through (UK HH:mm). */
    public static final String DEFAULT_JOIN_UK_HM = "14:30";
    public static final String DEFAULT_DOP_OPEN_UK_HM = "14:30";

    private static Path defaultSeedRoot() {
        String env = System.getenv("LVN_SEED_ROOT");
        if (env != null && !env.isBlank()) {
            return Path.of(env.trim());
        }
        return Path.of("C:/Work/Mat/bookmap/data/irt");
    }

    /** Repo root containing {@code research.irt_lvn} (parent of {@code data/irt}). */
    public static Path repoRoot() {
        Path irt = DEFAULT_SEED_ROOT;
        Path data = irt.getParent();
        if (data != null && data.getParent() != null) {
            return data.getParent();
        }
        return Path.of("C:/Work/Mat/bookmap");
    }

    public static final class MarketKnobs {
        public int windowDays;
        public int sensitivity;
        /** UK HH:MM join / seed through. */
        public String joinUkHm = DEFAULT_JOIN_UK_HM;
        public String dopOpenUkHm = DEFAULT_DOP_OPEN_UK_HM;
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
        /** Last approach-alert reason for the console footer (empty until first fire). */
        public final String alertFooter;
        public final LocalDate sessionDate;
        public final LvnApproachAlert.Config alert;
        /** Non-blank when a seed is missing, failed to load, or does not cover the session day. */
        public final String seedBanner;

        Snapshot(MarketLevels es, MarketLevels nq,
                 MarketKnobs esKnobs, MarketKnobs nqKnobs,
                 boolean showLvn, boolean showHvn, boolean showDop,
                 Path esSeedPath, Path nqSeedPath,
                 String statusDetail, String lastEvent, String alertFooter,
                 LocalDate sessionDate,
                 LvnApproachAlert.Config alert, String seedBanner) {
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
            this.alertFooter = alertFooter == null ? "" : alertFooter;
            this.sessionDate = sessionDate;
            this.alert = alert;
            this.seedBanner = seedBanner == null ? "" : seedBanner;
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
    private static volatile LocalDateTime esSeedThrough;
    private static volatile LocalDateTime nqSeedThrough;
    private static volatile String esSeedIssue = "";
    private static volatile String nqSeedIssue = "";
    private static volatile String seedBanner = "";
    private static volatile MarketLevels esLevels;
    private static volatile MarketLevels nqLevels;
    private static volatile double esDop = Double.NaN;
    private static volatile double nqDop = Double.NaN;
    private static volatile LocalDate sessionDateUk;
    private static volatile String statusDetail = "Auto-loading VAP seeds (ES / NQ)…";
    private static volatile String lastEvent = "";
    /** Human reason for the last approach alert — console footer only. */
    private static volatile String alertFooter = "";
    private static volatile LvnStackFile.Stack esStack;
    private static volatile LvnStackFile.Stack nqStack;

    private static final ConcurrentHashMap<String, Integer> ATTACH = new ConcurrentHashMap<>();
    private static final List<Consumer<Snapshot>> LISTENERS = new CopyOnWriteArrayList<>();
    private static final List<Runnable> APPLY_HOOKS = new CopyOnWriteArrayList<>();
    private static final LvnApproachAlert APPROACH_ALERT = new LvnApproachAlert();
    private static final AtomicBoolean AUTO_SEED_TRIED = new AtomicBoolean(false);
    private static final AtomicBoolean SEED_REBUILD_BUSY = new AtomicBoolean(false);
    /** Another rebuild was requested while one was running — run again when free. */
    private static final AtomicBoolean SEED_REBUILD_AGAIN = new AtomicBoolean(false);
    /** Last session day for which {@link #rebuildSeedsForSession} completed successfully. */
    private static volatile LocalDate completedSeedRebuildDay;

    static {
        APPROACH_ALERT.setListener(ev -> {
            lastEvent = ev.message;
            if (ev.reason != null && !ev.reason.isEmpty()) {
                alertFooter = ev.reason;
            }
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
        dest.joinUkHm = in.joinUkHm == null || in.joinUkHm.isBlank()
                ? DEFAULT_JOIN_UK_HM : in.joinUkHm.trim();
        dest.dopOpenUkHm = in.dopOpenUkHm == null || in.dopOpenUkHm.isBlank()
                ? DEFAULT_DOP_OPEN_UK_HM : in.dopOpenUkHm.trim();
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

    /**
     * Once per process: bind any existing {@code vap_seed_ES} / {@code vap_seed_NQ}
     * under {@link #DEFAULT_SEED_ROOT}, then kick an automatic session seed rebuild
     * (same as the console button). Missing seeds show on the banner until rebuild
     * finishes.
     */
    public static void ensureDefaultSeedsLoaded() {
        if (!AUTO_SEED_TRIED.compareAndSet(false, true)) {
            refreshSeedBanner();
            statusDetail = refreshStatus();
            fire();
            return;
        }
        tryAutoBind("ES");
        tryAutoBind("NQ");
        refreshSeedBanner();
        statusDetail = refreshStatus();
        fire();
        requestSessionSeedRebuild("attach");
    }

    /**
     * Rebuild ES+NQ VAP seeds for {@link #sessionDate()} through the ES Join UK time.
     * No-ops if a successful rebuild already completed for that day (unless a rebuild
     * is already running — then a follow-up is queued). Safe from EDT / feed threads.
     */
    public static void requestSessionSeedRebuild(String via) {
        LocalDate day = sessionDate();
        if (day.equals(completedSeedRebuildDay) && !SEED_REBUILD_BUSY.get()) {
            Log.info("LVN Levels seed rebuild skip day=" + day + " via=" + via
                    + " (already rebuilt)");
            return;
        }
        String through = ES_KNOBS.joinUkHm;
        if (through == null || through.isBlank()) {
            through = DEFAULT_JOIN_UK_HM;
        }
        Log.info("LVN Levels seed rebuild request day=" + day + " through=" + through
                + " via=" + via);
        rebuildSeedsForSession(through, null);
    }

    private static void tryAutoBind(String market) {
        if (seedPathFor(market) != null) {
            return;
        }
        Path dir = DEFAULT_SEED_ROOT.resolve("vap_seed_" + market);
        if (!Files.isDirectory(dir) || !Files.isRegularFile(dir.resolve("meta.json"))) {
            setSeedIssue(market, "cannot load seed — missing " + dir
                    + " (build with build_vap_seed or use Load " + market + " seed…)");
            Log.info("LVN Levels auto-seed missing " + market + " path=" + dir);
            return;
        }
        setSeedIssue(market, "");
        bindSeed(market, dir, "auto");
    }

    public static boolean seedRebuildBusy() {
        return SEED_REBUILD_BUSY.get();
    }

    /**
     * Run {@code build_vap_seed} for the current UK session day + Join time, then
     * reload ES/NQ seeds. Safe to call from the EDT — work runs on a daemon thread.
     *
     * @param throughUkHm Join / seed cut HH:mm (blank → {@link #DEFAULT_JOIN_UK_HM})
     * @param onUi optional UI callback (success/failure message), invoked on EDT by caller if needed
     */
    public static void rebuildSeedsForSession(String throughUkHm, Consumer<String> onUi) {
        if (!SEED_REBUILD_BUSY.compareAndSet(false, true)) {
            SEED_REBUILD_AGAIN.set(true);
            if (onUi != null) {
                onUi.accept("Seed rebuild already running — queued another pass");
            }
            return;
        }
        String through = throughUkHm == null || throughUkHm.isBlank()
                ? DEFAULT_JOIN_UK_HM : throughUkHm.trim();
        LocalDate day = sessionDate();
        lastEvent = "Rebuilding seeds for " + day + " through " + through + "…";
        fire();
        Thread t = new Thread(() -> {
            String msg;
            try {
                msg = runBuildVapSeed(day, through);
                Path esDir = DEFAULT_SEED_ROOT.resolve("vap_seed_ES");
                Path nqDir = DEFAULT_SEED_ROOT.resolve("vap_seed_NQ");
                bindSeedLoaded("ES", esDir, "rebuild");
                bindSeedLoaded("NQ", nqDir, "rebuild");
                completedSeedRebuildDay = day;
                lastEvent = "Seeds rebuilt for " + day + " through " + through;
                Log.info("LVN Levels " + lastEvent + " | " + msg);
                if (onUi != null) {
                    onUi.accept(lastEvent);
                }
            } catch (Exception ex) {
                msg = "Seed rebuild failed: " + shorten(ex.getMessage(), 240);
                Log.info("LVN Levels seed rebuild failed: " + ex.getMessage());
                lastEvent = msg;
                setSeedIssue("ES", msg);
                setSeedIssue("NQ", msg);
                refreshSeedBanner();
                fire();
                if (onUi != null) {
                    onUi.accept(msg);
                }
            } finally {
                boolean again = SEED_REBUILD_AGAIN.compareAndSet(true, false);
                SEED_REBUILD_BUSY.set(false);
                fire();
                if (again) {
                    // Session day may have arrived (replay) while the first pass ran.
                    String againThrough = ES_KNOBS.joinUkHm;
                    if (againThrough == null || againThrough.isBlank()) {
                        againThrough = DEFAULT_JOIN_UK_HM;
                    }
                    rebuildSeedsForSession(againThrough, onUi);
                }
            }
        }, "LvnSeedRebuild");
        t.setDaemon(true);
        t.start();
    }

    private static String runBuildVapSeed(LocalDate day, String throughHm) throws Exception {
        Path repo = repoRoot();
        Path venvPy = repo.resolve(".venv").resolve("Scripts").resolve("python.exe");
        String py = Files.isRegularFile(venvPy) ? venvPy.toString() : "python";
        List<String> cmd = new ArrayList<>();
        cmd.add(py);
        cmd.add("-m");
        cmd.add("research.irt_lvn.build_vap_seed");
        cmd.add("--market");
        cmd.add("both");
        cmd.add("--day");
        cmd.add(day.toString());
        cmd.add("--through");
        cmd.add(throughHm);
        cmd.add("--out");
        cmd.add(DEFAULT_SEED_ROOT.toAbsolutePath().toString());
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.directory(repo.toFile());
        pb.redirectErrorStream(true);
        // Avoid Windows cp1252 crashing on non-ASCII print from Python.
        pb.environment().put("PYTHONIOENCODING", "utf-8");
        pb.environment().put("PYTHONUTF8", "1");
        Log.info("LVN Levels seed rebuild: " + String.join(" ", cmd) + " cwd=" + repo);
        Process proc = pb.start();
        StringBuilder out = new StringBuilder();
        try (BufferedReader br = new BufferedReader(
                new InputStreamReader(proc.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = br.readLine()) != null) {
                out.append(line).append('\n');
                Log.info("build_vap_seed: " + line);
            }
        }
        int code = proc.waitFor();
        if (code != 0) {
            throw new IllegalStateException("build_vap_seed exit " + code
                    + " — " + shorten(out.toString(), 400));
        }
        return out.toString().trim();
    }

    private static String shorten(String s, int max) {
        if (s == null) {
            return "";
        }
        String t = s.trim().replace('\n', ' ');
        return t.length() <= max ? t : t.substring(0, max) + "…";
    }

    private static Path seedPathFor(String market) {
        return "NQ".equalsIgnoreCase(market) ? nqSeedPath : esSeedPath;
    }

    private static void setSeedIssue(String market, String issue) {
        String msg = issue == null ? "" : issue;
        if ("NQ".equalsIgnoreCase(market)) {
            nqSeedIssue = msg;
        } else {
            esSeedIssue = msg;
        }
    }

    private static void setSeedThrough(String market, LocalDateTime through) {
        if ("NQ".equalsIgnoreCase(market)) {
            nqSeedThrough = through;
        } else {
            esSeedThrough = through;
        }
    }

    /** Recompute banner from load issues + session vs seed-through coverage. */
    private static void refreshSeedBanner() {
        List<String> parts = new ArrayList<>(4);
        appendSeedBannerPart(parts, "ES", esSeedPath, esSeedThrough, esSeedIssue, esLevels);
        appendSeedBannerPart(parts, "NQ", nqSeedPath, nqSeedThrough, nqSeedIssue, nqLevels);
        seedBanner = String.join("   |   ", parts);
    }

    private static void appendSeedBannerPart(
            List<String> parts,
            String mkt,
            Path path,
            LocalDateTime through,
            String issue,
            MarketLevels lv) {
        if (issue != null && !issue.isEmpty()) {
            parts.add(mkt + ": " + issue);
            return;
        }
        if (path == null) {
            // Still loading or not attempted — no banner noise.
            return;
        }
        // Only judge coverage after the feed has set a UK session day (replay/live).
        LocalDate session = sessionDateUk;
        if (session != null && through != null && session.isAfter(through.toLocalDate())) {
            parts.add(mkt + ": seed through " + through.toLocalDate()
                    + " does not cover session " + session
                    + " — rebuild seed --day " + session);
            return;
        }
        if (lv != null && lv.warning != null && !lv.warning.isEmpty()) {
            String w = lv.warning;
            boolean notable = w.contains("STALE")
                    || w.toLowerCase(Locale.ROOT).contains("stale")
                    || w.contains("no minute seed")
                    || w.contains("full daily");
            if (notable) {
                parts.add(mkt + ": " + w);
            }
        }
    }

    /** Parse the seed file off the calling (EDT) thread, then bind. */
    public static void bindSeed(String market, Path path, String via) {
        Thread t = new Thread(() -> bindSeedLoaded(market, path, via), "LvnSeedLoad-" + market);
        t.setDaemon(true);
        t.start();
    }

    private static synchronized void bindSeedLoaded(String market, Path path, String via) {
        String mkt = market == null ? "" : market.trim().toUpperCase(Locale.ROOT);
        try {
            LvnVapSeedFile.Seed seed = LvnVapSeedFile.load(path);
            if (seed.meta.market != null && !seed.meta.market.isBlank()
                    && !seed.meta.market.equalsIgnoreCase(mkt)) {
                lastEvent = "Seed rejected: file is " + seed.meta.market
                        + " seed, not " + mkt + " (" + seed.root.getFileName() + ")";
                setSeedIssue(mkt, "wrong market in seed (" + seed.meta.market
                        + ") — expected " + mkt);
                Log.info("LVN Levels " + lastEvent);
                statusDetail = refreshStatus();
                refreshSeedBanner();
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
            setSeedThrough(mkt, seed.meta.throughUk);
            setSeedIssue(mkt, "");
            // Do NOT detect nodes here — Bookmap "Loading events" shares this thread.
            lastEvent = market + " seed loaded " + seed.root.getFileName()
                    + " through=" + knobs.joinUkHm + " (nodes on realtime/Apply)";
            Log.info("LVN Levels seed bound " + market + " via=" + via
                    + " root=" + seed.root + " through=" + seed.meta.throughUk);
            statusDetail = refreshStatus();
            refreshSeedBanner();
            fire();
            runApplyHooks();
        } catch (Exception e) {
            Log.info("LVN Levels seed bind failed: " + e.getMessage());
            lastEvent = "Seed load failed: " + e.getMessage();
            setSeedIssue(mkt.isEmpty() ? "ES" : mkt, "cannot load seed — " + e.getMessage());
            refreshSeedBanner();
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
                "Alert %s mode=%s ES approach=%.1f pullback=%.1f prox=%.1f | NQ approach=%.1f pullback=%.1f prox=%.1f cool=%ds LVN=%s DOP=%s sound=%s (%s)",
                c.enabled ? "ON" : "OFF", c.mode,
                c.esApproachPts, c.esPullbackPts, c.esCorrProxPts,
                c.nqApproachPts, c.nqPullbackPts, c.nqCorrProxPts,
                c.cooldownSec, c.watchLvn, c.watchDop, c.sound, via);
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
            alertFooter = "";
        }
        if (ES_VAP.hasSeed()) {
            ES_VAP.setJoinThrough(LocalDateTime.of(day, LvnLiveVap.parseUkHm(ES_KNOBS.joinUkHm)));
        }
        if (NQ_VAP.hasSeed()) {
            NQ_VAP.setJoinThrough(LocalDateTime.of(day, LvnLiveVap.parseUkHm(NQ_KNOBS.joinUkHm)));
        }
        lastEvent = "Session date " + day;
        statusDetail = refreshStatus();
        refreshSeedBanner();
        fire();
        // Rebuild for the feed's UK day (replay may differ from wall-clock used at attach).
        requestSessionSeedRebuild(prev == null ? "session" : "session-roll");
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
        refreshSeedBanner();
        fire();
        return lv;
    }

    public static Snapshot snapshot() {
        return new Snapshot(
                esLevels, nqLevels, ES_KNOBS.copy(), NQ_KNOBS.copy(),
                SHOW_LVN.get(), SHOW_HVN.get(), SHOW_DOP.get(),
                esSeedPath, nqSeedPath, statusDetail, lastEvent, alertFooter, sessionDate(),
                APPROACH_ALERT.config(), seedBanner);
    }

    public static String seedBanner() {
        return seedBanner == null ? "" : seedBanner;
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
