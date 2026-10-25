package com.bookmap.orderflow.trade;

import java.nio.file.Path;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

import com.bookmap.orderflow.core.LvnLevelFile;
import com.bookmap.orderflow.core.OpenClusterDetector;

import velox.api.layer1.common.Log;

/**
 * Process-wide settings + LVN levels for LVN Confluence AutoTrade.
 * Console writes; module reads; listeners refresh the UI.
 */
public final class LvnStrategyState {

    public enum Status {
        NO_LEVELS,
        LEVELS_LOADED,
        ARMED,
        IN_POSITION,
        HALTED
    }

    public static final class Settings {
        public double slPts = 8.0;
        public double tpPts = 24.0;
        public double offsetPts = 2.0;
        public double nearLvnPts = 10.0;
        public double minOpenDistPts = 5.0;
        public int orderSize = 1;
        /** UK minutes-of-day. */
        public int entryUntilUkMin = 1020; // 17:00
        public int eosUkMin = 1260; // 21:00
        public int snapUkMin = 870; // 14:30
        public double killMaxSessionLossPts = 150.0;
    }

    public static final class Snapshot {
        public final Status status;
        public final String statusDetail;
        /** @deprecated use {@link #esLevels} — kept as ES alias for older callers */
        public final LvnLevelFile.Levels levels;
        public final LvnLevelFile.Levels esLevels;
        public final LvnLevelFile.Levels nqLevels;
        public final Path esPath;
        public final Path nqPath;
        public final Settings settings;
        public final boolean tradingEnabled;
        public final boolean allowLongs;
        public final boolean allowShorts;
        public final boolean showLvnsOnChart;
        public final boolean showHvnsOnChart;
        /** UK session/replay date used to resolve LVNs from chart exports (null = calendar today). */
        public final LocalDate sessionDate;
        public final boolean stale;
        public final String warning;
        public final List<OpenClusterDetector.DisplayRow> rows;
        public final double midPts;
        public final int position;
        public final String lastEvent;
        /** {@code ES} / {@code NQ} / empty — market feeding the large-order table. */
        public final String liveBookMarket;
        public final boolean esAttached;
        public final boolean nqAttached;

        Snapshot(Status status, String statusDetail,
                 LvnLevelFile.Levels esLevels, LvnLevelFile.Levels nqLevels,
                 Path esPath, Path nqPath, Settings settings,
                 boolean tradingEnabled, boolean allowLongs, boolean allowShorts,
                 boolean showLvnsOnChart, boolean showHvnsOnChart,
                 LocalDate sessionDate, boolean stale, String warning,
                 List<OpenClusterDetector.DisplayRow> rows, double midPts, int position, String lastEvent,
                 String liveBookMarket, boolean esAttached, boolean nqAttached) {
            this.status = status;
            this.statusDetail = statusDetail;
            this.esLevels = esLevels;
            this.nqLevels = nqLevels;
            this.levels = esLevels;
            this.esPath = esPath;
            this.nqPath = nqPath;
            this.settings = settings;
            this.tradingEnabled = tradingEnabled;
            this.allowLongs = allowLongs;
            this.allowShorts = allowShorts;
            this.showLvnsOnChart = showLvnsOnChart;
            this.showHvnsOnChart = showHvnsOnChart;
            this.sessionDate = sessionDate;
            this.stale = stale;
            this.warning = warning;
            this.rows = rows;
            this.midPts = midPts;
            this.position = position;
            this.lastEvent = lastEvent;
            this.liveBookMarket = liveBookMarket == null ? "" : liveBookMarket;
            this.esAttached = esAttached;
            this.nqAttached = nqAttached;
        }
    }

    @FunctionalInterface
    public interface Flattener {
        void flatten();
    }

    @FunctionalInterface
    public interface CancelPending {
        void cancelPendingLimits();
    }

    private static final AtomicBoolean TRADING = new AtomicBoolean(false);
    private static final AtomicBoolean ALLOW_LONGS = new AtomicBoolean(true);
    private static final AtomicBoolean ALLOW_SHORTS = new AtomicBoolean(true);
    private static final AtomicBoolean HALTED = new AtomicBoolean(false);
    /** When true, attached modules plot LVN2/LVN3 on their chart. */
    private static final AtomicBoolean SHOW_LVNS_ON_CHART = new AtomicBoolean(true);
    /** When true, attached modules plot HVN1/2/3 from the same export row. */
    private static final AtomicBoolean SHOW_HVNS_ON_CHART = new AtomicBoolean(false);

    private static final Settings SETTINGS = new Settings();
    private static volatile LvnLevelFile.Levels esLevels;
    private static volatile LvnLevelFile.Levels nqLevels;
    private static volatile Path esPath;
    private static volatile Path nqPath;
    /** Replay/live feed UK date — LVNs resolve as-of this day @14:30 from chart exports. */
    private static volatile LocalDate sessionDateUk;
    private static volatile Status status = Status.NO_LEVELS;
    private static volatile String statusDetail = "Load ES / NQ IRT chart exports";
    private static volatile String warning = "";
    private static volatile String lastEvent = "";
    private static volatile double midPts;
    private static volatile int position;
    private static volatile List<OpenClusterDetector.DisplayRow> rows = List.of();
    private static volatile boolean snapped;
    /** Which market last fed the live large-order table ({@code ES} preferred over {@code NQ}). */
    private static volatile String liveBookMarket = "";
    private static volatile String liveBookAlias = "";
    /** Attach counts by role so dual-chart NQ/ES does not drop the other on stop. */
    private static final ConcurrentHashMap<String, Integer> ATTACH_COUNTS = new ConcurrentHashMap<>();

    private static final List<Consumer<Snapshot>> LISTENERS = new CopyOnWriteArrayList<>();
    private static final List<Flattener> FLATTENERS = new CopyOnWriteArrayList<>();
    private static final List<CancelPending> CANCELERS = new CopyOnWriteArrayList<>();
    private static final List<Runnable> RESET_DAY = new CopyOnWriteArrayList<>();

    private LvnStrategyState() {}

    public static void addListener(Consumer<Snapshot> listener) {
        if (listener != null) {
            LISTENERS.add(listener);
            listener.accept(snapshot());
        }
    }

    public static void removeListener(Consumer<Snapshot> listener) {
        LISTENERS.remove(listener);
    }

    public static void registerFlattener(Flattener f) {
        if (f != null) {
            FLATTENERS.add(f);
        }
    }

    public static void unregisterFlattener(Flattener f) {
        FLATTENERS.remove(f);
    }

    public static void registerCancelPending(CancelPending c) {
        if (c != null) {
            CANCELERS.add(c);
        }
    }

    public static void unregisterCancelPending(CancelPending c) {
        CANCELERS.remove(c);
    }

    public static void registerAttach(String role, String alias) {
        if (role == null || role.isEmpty()) {
            return;
        }
        ATTACH_COUNTS.merge(role, 1, Integer::sum);
        Log.info("LVN attach +" + role + (alias != null ? " alias=" + alias : "")
                + " counts=" + ATTACH_COUNTS);
        refreshWarnings();
        fire();
    }

    public static void unregisterAttach(String role) {
        if (role == null) {
            return;
        }
        ATTACH_COUNTS.computeIfPresent(role, (k, v) -> v <= 1 ? null : v - 1);
        if (role.equals(liveBookMarket) && !esAttached() && !nqAttached()) {
            liveBookMarket = "";
            liveBookAlias = "";
            rows = List.of();
            midPts = 0;
        } else if (role.equals(liveBookMarket) && "ES".equals(role) && !esAttached()) {
            liveBookMarket = "";
            liveBookAlias = "";
            rows = List.of();
            midPts = 0;
        }
        Log.info("LVN attach -" + role + " counts=" + ATTACH_COUNTS);
        refreshWarnings();
        fire();
    }

    public static boolean esAttached() {
        return ATTACH_COUNTS.getOrDefault("ES", 0) > 0;
    }

    public static boolean nqAttached() {
        return ATTACH_COUNTS.getOrDefault("NQ", 0) > 0;
    }

    public static String liveBookMarket() {
        return liveBookMarket;
    }

    public static void registerResetDay(Runnable r) {
        if (r != null) {
            RESET_DAY.add(r);
        }
    }

    public static void unregisterResetDay(Runnable r) {
        RESET_DAY.remove(r);
    }

    public static synchronized Settings settings() {
        Settings s = new Settings();
        s.slPts = SETTINGS.slPts;
        s.tpPts = SETTINGS.tpPts;
        s.offsetPts = SETTINGS.offsetPts;
        s.nearLvnPts = SETTINGS.nearLvnPts;
        s.minOpenDistPts = SETTINGS.minOpenDistPts;
        s.orderSize = SETTINGS.orderSize;
        s.entryUntilUkMin = SETTINGS.entryUntilUkMin;
        s.eosUkMin = SETTINGS.eosUkMin;
        s.snapUkMin = SETTINGS.snapUkMin;
        s.killMaxSessionLossPts = SETTINGS.killMaxSessionLossPts;
        return s;
    }

    public static synchronized void applySettings(Settings in, String via) {
        if (in == null) {
            return;
        }
        Log.info(String.format(
                "LVN settings apply (%s): SL %.1f→%.1f TP %.1f→%.1f offset %.1f→%.1f near %.1f→%.1f "
                        + "minOpen %.1f→%.1f size %d→%d entryUntil %d→%d eos %d→%d",
                via, SETTINGS.slPts, in.slPts, SETTINGS.tpPts, in.tpPts,
                SETTINGS.offsetPts, in.offsetPts, SETTINGS.nearLvnPts, in.nearLvnPts,
                SETTINGS.minOpenDistPts, in.minOpenDistPts,
                SETTINGS.orderSize, in.orderSize,
                SETTINGS.entryUntilUkMin, in.entryUntilUkMin,
                SETTINGS.eosUkMin, in.eosUkMin));
        SETTINGS.slPts = Math.max(0.0, in.slPts);
        SETTINGS.tpPts = Math.max(0.25, in.tpPts);
        SETTINGS.offsetPts = Math.max(0.0, in.offsetPts);
        SETTINGS.nearLvnPts = Math.max(0.0, in.nearLvnPts);
        SETTINGS.minOpenDistPts = Math.max(0.0, in.minOpenDistPts);
        SETTINGS.orderSize = Math.max(1, in.orderSize);
        SETTINGS.entryUntilUkMin = in.entryUntilUkMin;
        SETTINGS.eosUkMin = in.eosUkMin;
        SETTINGS.snapUkMin = in.snapUkMin;
        SETTINGS.killMaxSessionLossPts = Math.max(0.0, in.killMaxSessionLossPts);
        fire();
    }

    /** ES levels (used for confluence trading). */
    public static LvnLevelFile.Levels levels() {
        return esLevels;
    }

    public static LvnLevelFile.Levels esLevels() {
        return esLevels;
    }

    public static LvnLevelFile.Levels nqLevels() {
        return nqLevels;
    }

    /** Levels for instrument role {@code "ES"} or {@code "NQ"}. */
    public static LvnLevelFile.Levels levelsFor(String market) {
        if (market == null) {
            return null;
        }
        if ("NQ".equalsIgnoreCase(market)) {
            return nqLevels;
        }
        if ("ES".equalsIgnoreCase(market)) {
            return esLevels;
        }
        return null;
    }

    public static Path levelsPath() {
        return esPath;
    }

    public static boolean showLvnsOnChart() {
        return SHOW_LVNS_ON_CHART.get();
    }

    public static void setShowLvnsOnChart(boolean on, String via) {
        SHOW_LVNS_ON_CHART.set(on);
        Log.info("LVN show on chart=" + on + " (" + via + ")");
        lastEvent = on ? "Show LVNs on chart ON" : "Show LVNs on chart OFF";
        fire();
    }

    public static boolean showHvnsOnChart() {
        return SHOW_HVNS_ON_CHART.get();
    }

    public static void setShowHvnsOnChart(boolean on, String via) {
        SHOW_HVNS_ON_CHART.set(on);
        Log.info("HVN show on chart=" + on + " (" + via + ")");
        lastEvent = on ? "Show HVNs on chart ON" : "Show HVNs on chart OFF";
        fire();
    }

    public static boolean tradingEnabled() {
        return TRADING.get() && !HALTED.get();
    }

    public static boolean rawTradingFlag() {
        return TRADING.get();
    }

    public static boolean isHalted() {
        return HALTED.get();
    }

    public static boolean allowLongs() {
        return ALLOW_LONGS.get();
    }

    public static boolean allowShorts() {
        return ALLOW_SHORTS.get();
    }

    public static boolean allowSide(int side) {
        return side > 0 ? allowLongs() : allowShorts();
    }

    public static boolean isSnapped() {
        return snapped;
    }

    /** Active session date for LVN as-of resolution (replay feed or live). */
    public static LocalDate sessionDate() {
        return sessionDateUk != null ? sessionDateUk : LocalDate.now(LvnLevelFile.UK);
    }

    /**
     * Called by the module from feed timestamps. When the UK date changes,
     * re-resolves LVN2/LVN3 from any bound IRT chart exports for that day
     * (last non-zero row at/before 14:30) so replay matches the backtest day.
     */
    public static synchronized void setSessionDateFromFeed(LocalDate feedDayUk) {
        if (feedDayUk == null) {
            return;
        }
        LocalDate prev = sessionDateUk;
        if (feedDayUk.equals(prev)) {
            return;
        }
        sessionDateUk = feedDayUk;
        Log.info("LVN session date=" + feedDayUk + " (was " + prev + ") — resolving chart exports");
        boolean changed = false;
        if (esPath != null) {
            changed |= resolvePath("ES", esPath, feedDayUk, "feed");
        }
        if (nqPath != null) {
            changed |= resolvePath("NQ", nqPath, feedDayUk, "feed");
        }
        if (!changed) {
            refreshWarnings();
            refreshStatusDetail();
            lastEvent = "Session date " + feedDayUk + " (no chart export bound yet)";
        }
        recomputeStatus();
        fire();
    }

    public static void setTradingEnabled(boolean on, String via) {
        if (on) {
            LvnLevelFile.Levels lv = esLevels;
            if (lv == null || !lv.hasAny()) {
                Log.info("LVN trading enable blocked — no ES levels (" + via + ")");
                lastEvent = "Enable blocked: load ES LVNs first";
                fire();
                return;
            }
            LocalDate need = sessionDate();
            if (!lv.date.equals(need)) {
                Log.info("LVN trading enable blocked — ES LVNs are for " + lv.date
                        + " but session is " + need + " (" + via + ")");
                lastEvent = "Enable blocked: ES LVNs date " + lv.date + " ≠ session " + need;
                fire();
                return;
            }
            if (HALTED.get()) {
                Log.info("LVN trading enable blocked — kill switch (" + via + ")");
                lastEvent = "Enable blocked: halted";
                fire();
                return;
            }
        }
        TRADING.set(on);
        Log.info("LVN trading=" + on + " (" + via + ")");
        lastEvent = on ? "Trading ON" : "Trading OFF";
        recomputeStatus();
        fire();
    }

    public static void setSides(boolean longs, boolean shorts, String via) {
        ALLOW_LONGS.set(longs);
        ALLOW_SHORTS.set(shorts);
        Log.info("LVN sides longs=" + longs + " shorts=" + shorts + " (" + via + ")");
        fire();
    }

    public static void setHalted(boolean halted, String via) {
        HALTED.set(halted);
        if (halted) {
            TRADING.set(false);
        }
        Log.info("LVN halted=" + halted + " (" + via + ")");
        lastEvent = halted ? "HALTED " + via : "Halt cleared";
        recomputeStatus();
        fire();
    }

    /**
     * Bind an IRT chart export (or simple CSV) for {@code market} and resolve
     * LVN2/LVN3 for the current session/replay date (as-of 14:30 UK that day).
     */
    public static synchronized void bindChartExport(String market, Path path, String via) {
        String mkt = market == null ? "ES" : market.trim().toUpperCase();
        if (!"ES".equals(mkt) && !"NQ".equals(mkt)) {
            Log.info("LVN bind rejected — market must be ES or NQ, got " + market);
            lastEvent = "Bind rejected: market " + market;
            fire();
            return;
        }
        if ("NQ".equals(mkt)) {
            nqPath = path;
        } else {
            esPath = path;
        }
        LocalDate day = sessionDate();
        if (!resolvePath(mkt, path, day, via)) {
            lastEvent = "Bind " + mkt + " failed for session " + day;
            recomputeStatus();
            fire();
        }
    }

    /** Load pre-parsed levels (tests / simple one-day CSV already resolved). */
    public static void loadLevels(String market, LvnLevelFile.Levels lv, Path path, String via) {
        String mkt = market == null ? "ES" : market.trim().toUpperCase();
        if (!"ES".equals(mkt) && !"NQ".equals(mkt)) {
            Log.info("LVN load rejected — market must be ES or NQ, got " + market);
            lastEvent = "Load rejected: market " + market;
            fire();
            return;
        }
        if ("NQ".equals(mkt)) {
            nqLevels = lv;
            nqPath = path;
        } else {
            esLevels = lv;
            esPath = path;
        }
        refreshWarnings();
        refreshStatusDetail();
        if (lv != null) {
            lastEvent = "Loaded " + mkt + " LVNs for " + lv.date + " via " + via;
            Log.info(String.format(
                    "LVN %s loaded: LVN2=%.2f LVN3=%.2f HVN1=%.2f HVN2=%.2f HVN3=%.2f file=%s date=%s note=%s",
                    mkt, lv.lvn2, lv.lvn3, lv.hvn1, lv.hvn2, lv.hvn3,
                    path != null ? path.getFileName() : "?", lv.date, lv.note));
        }
        recomputeStatus();
        fire();
    }

    /** @deprecated prefer {@link #bindChartExport} or {@link #loadLevels(String, LvnLevelFile.Levels, Path, String)} */
    public static void loadLevels(LvnLevelFile.Levels lv, Path path, String via) {
        loadLevels("ES", lv, path, via);
    }

    public static void clearLevels(String via) {
        esLevels = null;
        nqLevels = null;
        esPath = null;
        nqPath = null;
        TRADING.set(false);
        status = Status.NO_LEVELS;
        statusDetail = "Load ES / NQ IRT chart exports";
        lastEvent = "Levels cleared (" + via + ")";
        fire();
    }

    private static boolean resolvePath(String mkt, Path path, LocalDate day, String via) {
        try {
            LvnLevelFile.Levels lv = LvnLevelFile.load(path, day);
            if (lv == null || !lv.hasAny()) {
                Log.info("LVN resolve " + mkt + " empty for " + day + " in " + path.getFileName());
                return false;
            }
            if ("NQ".equals(mkt)) {
                nqLevels = lv;
            } else {
                esLevels = lv;
            }
            refreshWarnings();
            refreshStatusDetail();
            lastEvent = String.format("%s LVNs as-of %s (session %s) via %s", mkt, lv.note, day, via);
            Log.info(String.format(
                    "LVN %s resolved session=%s LVN2=%.2f LVN3=%.2f HVN1=%.2f HVN2=%.2f HVN3=%.2f file=%s note=%s",
                    mkt, day, lv.lvn2, lv.lvn3, lv.hvn1, lv.hvn2, lv.hvn3,
                    path.getFileName(), lv.note));
            recomputeStatus();
            fire();
            return true;
        } catch (Exception ex) {
            Log.info("LVN resolve " + mkt + " failed for " + day + ": " + ex.getMessage());
            warning = mkt + " resolve failed for " + day + ": " + ex.getMessage();
            lastEvent = warning;
            return false;
        }
    }

    private static void refreshWarnings() {
        LocalDate need = sessionDate();
        StringBuilder w = new StringBuilder();
        if (!esAttached()) {
            w.append("Attach add-on on an ES chart for large orders / 14:30 snap / trading");
            if (nqAttached()) {
                w.append(" (NQ attach shows NQ LVN lines + NQ book only)");
            }
        }
        if (esLevels != null && !esLevels.date.equals(need)) {
            if (w.length() > 0) {
                w.append(" | ");
            }
            w.append("ES LVNs are for ").append(esLevels.date)
                    .append(" (session ").append(need).append(")");
        }
        if (nqLevels != null && !nqLevels.date.equals(need)) {
            if (w.length() > 0) {
                w.append(" | ");
            }
            w.append("NQ LVNs are for ").append(nqLevels.date)
                    .append(" (session ").append(need).append(")");
        }
        warning = w.toString();
    }

    private static void refreshStatusDetail() {
        StringBuilder sb = new StringBuilder();
        sb.append("Session ").append(sessionDate()).append("  |  ");
        if (esLevels != null && esLevels.hasAny()) {
            sb.append(String.format("ES LVN2=%.2f LVN3=%.2f", esLevels.lvn2, esLevels.lvn3));
            if (esPath != null) {
                sb.append(" [").append(esPath.getFileName()).append("]");
            }
        } else {
            sb.append("ES: —");
        }
        sb.append("   |   ");
        if (nqLevels != null && nqLevels.hasAny()) {
            sb.append(String.format("NQ LVN2=%.2f LVN3=%.2f", nqLevels.lvn2, nqLevels.lvn3));
            if (nqPath != null) {
                sb.append(" [").append(nqPath.getFileName()).append("]");
            }
        } else {
            sb.append("NQ: —");
        }
        statusDetail = sb.toString();
    }

    /**
     * Push live book clusters into the console. ES attach always wins; NQ only
     * updates the table when no ES module is attached (or ES has not published yet).
     */
    public static void publishLive(
            String market,
            String alias,
            List<OpenClusterDetector.DisplayRow> displayRows,
            double mid,
            int pos,
            boolean isSnapped,
            String eventOrNull) {
        String mkt = market == null ? "" : market.trim().toUpperCase();
        boolean fromEs = "ES".equals(mkt);
        // Once ES has published, ignore NQ row updates so the console stays on ES book
        if (!fromEs && esAttached() && "ES".equals(liveBookMarket)) {
            return;
        }
        liveBookMarket = mkt;
        liveBookAlias = alias == null ? "" : alias;
        rows = displayRows == null ? List.of() : List.copyOf(displayRows);
        midPts = mid;
        if (fromEs) {
            position = pos;
            snapped = isSnapped;
        }
        if (eventOrNull != null && !eventOrNull.isEmpty()) {
            lastEvent = eventOrNull;
        }
        if (fromEs && esLevels != null && mid > 0) {
            String bw = LvnLevelFile.basisWarning(esLevels, mid);
            if (bw != null) {
                warning = "ES: " + bw;
            } else {
                refreshWarnings();
            }
        }
        recomputeStatus();
        fire();
    }

    /** @deprecated prefer {@link #publishLive(String, String, List, double, int, boolean, String)} */
    public static void publishLive(
            List<OpenClusterDetector.DisplayRow> displayRows,
            double mid,
            int pos,
            boolean isSnapped,
            String eventOrNull) {
        publishLive(liveBookMarket.isEmpty() ? "ES" : liveBookMarket, liveBookAlias,
                displayRows, mid, pos, isSnapped, eventOrNull);
    }

    public static void flattenAll() {
        setTradingEnabled(false, "flatten");
        if (FLATTENERS.isEmpty()) {
            Log.info("LVN FLATTEN: no module registered");
            lastEvent = "Flatten: no module";
            fire();
            return;
        }
        Log.info("LVN FLATTEN: " + FLATTENERS.size() + " instance(s)");
        for (Flattener f : FLATTENERS) {
            try {
                f.flatten();
            } catch (Throwable t) {
                Log.info("LVN FLATTEN failed: " + t);
            }
        }
        lastEvent = "Flatten requested";
        fire();
    }

    public static void cancelAllPending() {
        if (CANCELERS.isEmpty()) {
            Log.info("LVN CANCEL: no module registered");
            return;
        }
        for (CancelPending c : CANCELERS) {
            try {
                c.cancelPendingLimits();
            } catch (Throwable t) {
                Log.info("LVN CANCEL failed: " + t);
            }
        }
        lastEvent = "Cancel pending limits";
        fire();
    }

    public static void resetDay(String via) {
        Log.info("LVN RESET DAY (" + via + ")");
        snapped = false;
        for (Runnable r : RESET_DAY) {
            try {
                r.run();
            } catch (Throwable t) {
                Log.info("LVN RESET failed: " + t);
            }
        }
        lastEvent = "Day reset";
        recomputeStatus();
        fire();
    }

    public static Snapshot snapshot() {
        LocalDate need = sessionDate();
        boolean stale = false;
        if (esLevels != null && !esLevels.date.equals(need)) {
            stale = true;
        }
        if (nqLevels != null && !nqLevels.date.equals(need)) {
            stale = true;
        }
        return new Snapshot(
                status, statusDetail, esLevels, nqLevels, esPath, nqPath, settings(),
                TRADING.get(), ALLOW_LONGS.get(), ALLOW_SHORTS.get(),
                SHOW_LVNS_ON_CHART.get(), SHOW_HVNS_ON_CHART.get(),
                need, stale, warning,
                new ArrayList<>(rows), midPts, position, lastEvent,
                liveBookMarket, esAttached(), nqAttached());
    }

    private static void recomputeStatus() {
        if (HALTED.get()) {
            status = Status.HALTED;
            return;
        }
        if (position != 0) {
            status = Status.IN_POSITION;
            return;
        }
        if (snapped) {
            status = Status.ARMED;
            return;
        }
        boolean any = (esLevels != null && esLevels.hasAny())
                || (nqLevels != null && nqLevels.hasAny());
        if (any) {
            status = Status.LEVELS_LOADED;
            return;
        }
        status = Status.NO_LEVELS;
    }

    private static void fire() {
        Snapshot snap = snapshot();
        for (Consumer<Snapshot> listener : LISTENERS) {
            try {
                listener.accept(snap);
            } catch (RuntimeException ex) {
                Log.info("LVN state listener failed: " + ex.getMessage());
            }
        }
    }
}
