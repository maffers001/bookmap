package com.bookmap.orderflow.trade;

import java.awt.Color;
import java.awt.Font;
import java.awt.FontMetrics;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.LvnLevelFile;
import com.bookmap.orderflow.core.OpenClusterDetector;
import com.bookmap.orderflow.core.OpenClusterDetector.ArmedLevel;
import com.bookmap.orderflow.core.OpenClusterDetector.LevelState;
import com.bookmap.orderflow.core.PriceTicks;

import velox.api.layer1.Layer1ApiAdminAdapter;
import velox.api.layer1.Layer1ApiProvider;
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1MultiAccountTradingSupported;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.annotations.Layer1TradingStrategy;
import velox.api.layer1.common.Log;
import velox.api.layer1.common.helper.AccountListManager;
import velox.api.layer1.data.AccountInfo;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.OrderCancelParameters;
import velox.api.layer1.data.OrderDuration;
import velox.api.layer1.data.OrderInfoUpdate;
import velox.api.layer1.data.OrderType;
import velox.api.layer1.data.SimpleOrderSendParameters;
import velox.api.layer1.data.SimpleOrderSendParametersBuilder;
import velox.api.layer1.data.StatusInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.messages.SelectedAccountChangedMessage;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyIndicator.GraphType;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalModeListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.OrdersListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.PositionListener;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * ES open-cluster ∩ IRT LVN confluence first-touch fade autotrade.
 *
 * <p>Frozen research book (console-adjustable SL/TP):
 * ES · LVN≤10 · outer wall · outside+2 · SL8/TP24 · min-open 5 ·
 * fills until 17:00 UK · EOS flatten 21:00.
 *
 * <p>LVN2/LVN3 are loaded via {@link LvnControlPanel} from an IRT export
 * before the open. Clusters are snapshotted from the live book at 14:30 UK.
 * Default: signals only ({@code enableTrading} panel flag via console).
 */
@Layer1SimpleAttachable
@Layer1TradingStrategy
@Layer1MultiAccountTradingSupported
@Layer1StrategyName("LVN Confluence AutoTrade (ES)")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class LvnConfluenceAutoTradeModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        TimeListener,
        IntervalListener,
        PositionListener,
        OrdersListener,
        HistoricalModeListener {

    @Parameter(name = "Verbose log")
    Boolean verboseLog = true;

    @Parameter(name = "Use selected account (dropdown)")
    Boolean useSelectedAccount = true;

    private Api api;
    private Layer1ApiProvider provider;
    private Layer1ApiAdminAdapter adminListener;
    private final AccountListManager accountListManager = new AccountListManager();
    private String primaryAlias;
    /** {@code ES} / {@code NQ} / {@code OTHER} — trading only on ES. */
    private String instrumentRole = "OTHER";
    private double pips = 0.25;
    private volatile boolean realtimeStarted;
    private long nowNs;
    private LocalDate feedDateUk;

    private OpenClusterDetector detector;
    private boolean snapDoneForDay;
    private LocalDate snapDay;
    private boolean cancelledAtEntryUntil;
    private boolean flattenedAtEos;
    private final Set<String> pendingEntryOrderIds = new HashSet<>();
    private final Map<String, ArmedLevel> orderToLevel = new HashMap<>();
    private final Map<String, Integer> filledByOrderId = new HashMap<>();

    private int position;
    private int netFills;
    private boolean flatLocked;
    private int openSide;
    private double entryFillPx;
    private volatile String tradingAccountId;
    private double sessionNetPts;
    private int sessionTrades;

    private Indicator signalIndicator;
    private Indicator longMarker;
    private Indicator shortMarker;
    private Indicator lvn2Marker;
    private Indicator lvn3Marker;
    private Indicator hvn1Marker;
    private Indicator hvn2Marker;
    private Indicator hvn3Marker;
    /** Last price we stamped an on-chart text icon for (re-stamp when it changes). */
    private double lvn2IconPx = Double.NaN;
    private double lvn3IconPx = Double.NaN;
    private double hvn1IconPx = Double.NaN;
    private double hvn2IconPx = Double.NaN;
    private double hvn3IconPx = Double.NaN;
    private boolean signalThisInterval;
    private long lastBookLogNs;
    private boolean loggedLvnPlot;

    /** All LVN lines — cyan. */
    private static final Color COL_LVN = new Color(0, 255, 255);
    /** All HVN lines — bright green. */
    private static final Color COL_HVN = new Color(0, 255, 80);

    private final LvnStrategyState.Flattener flattener = this::doFlatten;
    private final LvnStrategyState.CancelPending canceler = this::cancelPendingLimits;
    private final Runnable resetDayHook = this::onResetDay;

    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final DateTimeFormatter TIME_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS z");

    // ------------------------------------------------------------ lifecycle

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.api = api;
        this.primaryAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        this.realtimeStarted = false;
        this.position = 0;
        this.netFills = 0;
        this.flatLocked = false;
        this.openSide = 0;
        this.entryFillPx = 0;
        this.sessionNetPts = 0;
        this.sessionTrades = 0;
        this.snapDoneForDay = false;
        this.cancelledAtEntryUntil = false;
        this.flattenedAtEos = false;
        this.pendingEntryOrderIds.clear();
        this.orderToLevel.clear();
        this.filledByOrderId.clear();

        instrumentRole = InstrumentClassifier.classify(info);
        rebuildDetector("init");

        // All indicators MUST be registered here — Bookmap forbids registerIndicator later.
        // Price text is stamped via addIcon when levels load (allowed outside initialize).
        signalIndicator = api.registerIndicator("LVN Signal", GraphType.BOTTOM, Double.NaN, true, true);
        longMarker = api.registerIndicator("Next Long", GraphType.PRIMARY, Double.NaN, true, true);
        shortMarker = api.registerIndicator("Next Short", GraphType.PRIMARY, Double.NaN, true, true);
        lvn2Marker = api.registerIndicator("LVN2", GraphType.PRIMARY, Double.NaN, true, true);
        lvn3Marker = api.registerIndicator("LVN3", GraphType.PRIMARY, Double.NaN, true, true);
        hvn1Marker = api.registerIndicator("HVN1", GraphType.PRIMARY, Double.NaN, true, true);
        hvn2Marker = api.registerIndicator("HVN2", GraphType.PRIMARY, Double.NaN, true, true);
        hvn3Marker = api.registerIndicator("HVN3", GraphType.PRIMARY, Double.NaN, true, true);
        signalIndicator.setColor(new Color(255, 180, 40));
        // Next Long / Next Short — both blue (distinct from cyan LVNs)
        Color nextBlue = new Color(30, 120, 255);
        longMarker.setColor(nextBlue);
        shortMarker.setColor(nextBlue);
        styleLevelLine(lvn2Marker, COL_LVN, 50);
        styleLevelLine(lvn3Marker, COL_LVN, 50);
        styleLevelLine(hvn1Marker, COL_HVN, 40);
        styleLevelLine(hvn2Marker, COL_HVN, 40);
        styleLevelLine(hvn3Marker, COL_HVN, 40);
        longMarker.setWidth(3);
        shortMarker.setWidth(3);
        longMarker.setRenderPriority(70);
        shortMarker.setRenderPriority(70);
        lvn2IconPx = lvn3IconPx = hvn1IconPx = hvn2IconPx = hvn3IconPx = Double.NaN;

        installAccountListener(api);
        refreshTradingAccountId("init");

        // Trading hooks only on ES; NQ attach is for chart LVN display + NQ book watch
        if (isEs()) {
            LvnStrategyState.registerFlattener(flattener);
            LvnStrategyState.registerCancelPending(canceler);
            LvnStrategyState.registerResetDay(resetDayHook);
        }
        if (isEs() || isNq()) {
            LvnStrategyState.registerAttach(instrumentRole, alias);
        }
        LvnControlPanel.showPanel();

        applyLevelsToDetector();

        Log.info("LVN init on " + alias
                + " role=" + instrumentRole
                + " pips=" + pips
                + " (console: Load ES/NQ LVNs + Show on chart; trading ES-only)");
        if (!isEs() && !isNq()) {
            Log.info("LVN WARN: attach on ES (trade) or NQ (display) — got " + instrumentRole);
        } else if (isNq()) {
            Log.info("LVN NQ attach: NQ LVN/HVN lines + NQ large-order watch "
                    + "(attach also on ES for snap/trading + ES lines)");
        }
    }

    private boolean isEs() {
        return "ES".equals(instrumentRole);
    }

    private boolean isNq() {
        return "NQ".equals(instrumentRole);
    }

    private void applyLevelsToDetector() {
        LvnLevelFile.Levels lv = LvnStrategyState.levelsFor(instrumentRole);
        if (lv != null && detector != null) {
            detector.setLvns(lv.lvn2, lv.lvn3);
        }
    }

    private void rebuildDetector(String via) {
        OpenClusterDetector.Config c = new OpenClusterDetector.Config();
        c.pips = pips;
        // Match research export_open_clusters MARKET_CFG (ES vs NQ)
        if (isNq()) {
            c.bookRadiusPts = 100.0;
            c.clusterTicks = 2;
            c.largeSizeMin = 15;
            c.nearLvnPts = 12.0; // NQ band research default; console can still override
        } else {
            c.bookRadiusPts = 60.0;
            c.clusterTicks = 6;
            c.largeSizeMin = 20;
        }
        LvnStrategyState.Settings s = LvnStrategyState.settings();
        c.offsetPts = s.offsetPts;
        c.nearLvnPts = s.nearLvnPts;
        c.minOpenDistPts = s.minOpenDistPts;
        detector = new OpenClusterDetector(c);
        applyLevelsToDetector();
        Log.info("LVN detector built (" + via + ") role=" + instrumentRole
                + " rad=" + c.bookRadiusPts + " clusterTicks=" + c.clusterTicks
                + " largeMin=" + c.largeSizeMin + " " + detector.summary());
    }

    private void onResetDay() {
        if (!isEs()) {
            return;
        }
        snapDoneForDay = false;
        snapDay = null;
        cancelledAtEntryUntil = false;
        flattenedAtEos = false;
        loggedLvnPlot = false;
        // Keep existing price-named lines; syncPriceLine re-registers if LVNs change
        cancelPendingLimits();
        detector.resetDay();
        rebuildDetector("resetDay");
        Log.info("LVN day reset — waiting for next 14:30 snap");
    }

    // ------------------------------------------------------------ market data

    @Override
    public void onTimestamp(long nanoseconds) {
        nowNs = nanoseconds;
        detector.onTimestamp(nanoseconds);
        // Drive LVN as-of date from feed (replay day or live) before snap
        LocalDate day = ukTime(nanoseconds).toLocalDate();
        LocalDate prevSession = LvnStrategyState.sessionDate();
        boolean dateChanged = feedDateUk == null || !feedDateUk.equals(day);
        feedDateUk = day;
        LvnStrategyState.setSessionDateFromFeed(day);
        if (dateChanged && isEs() && !day.equals(prevSession)) {
            // New replay/live day → re-arm with that day's LVNs from chart export
            onResetDay();
        }
        applyLevelsToDetector();
        maybeSnap(nanoseconds);
        maybeSessionOps(nanoseconds);
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        // Bookmap depth prices are typically tick indices on this install
        int tick = price;
        if (price < 80_000 && pips > 0) {
            // already tick-like small? treat as ticks anyway for ES (~30k)
            tick = price;
        }
        detector.onDepth(isBid, tick, size);
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        // Align tape tick to depth book when the 80k heuristic mis-classifies ES (~30k ticks).
        // Only accept a candidate within 500 ticks of a valid mid — never chase a broken BBO.
        int tick = PriceTicks.apiPriceToTick(price, pips);
        Integer bbo = detector.midTick();
        if (bbo != null && pips > 0) {
            int asChart = (int) Math.round(price / pips);
            int asRaw = (int) Math.round(price);
            int best = tick;
            int bestDist = Math.abs(tick - bbo);
            int dChart = Math.abs(asChart - bbo);
            if (dChart < bestDist) {
                best = asChart;
                bestDist = dChart;
            }
            int dRaw = Math.abs(asRaw - bbo);
            if (dRaw < bestDist) {
                best = asRaw;
                bestDist = dRaw;
            }
            if (bestDist < 500) {
                tick = best;
            }
        }
        detector.onTrade(tick, nowNs);
        if (!isEs() || !detector.isSnapped()) {
            return;
        }
        // While in a trade, entries are cancelled by design — tape touching the
        // next level must NOT consume it (it gets re-rested when flat).
        if (inTrade()) {
            return;
        }
        // Research fills when tape reaches entry; resting limits handle live.
        // Chart px from the book-aligned tick: ES tape ticks are ~31k (<80k), so
        // PriceTicks.toChartPoints would return the raw tick (31115) as "points"
        // and spuriously hit every ASK level. tick * pips is the real price.
        double px = tick * (pips > 0 ? pips : 0.25);
        for (ArmedLevel a : detector.armedLevels()) {
            if (!a.isTradeable()) {
                continue;
            }
            boolean hit = a.side == OpenClusterDetector.Side.BID
                    ? px <= a.entryPts
                    : px >= a.entryPts;
            if (!hit) {
                continue;
            }
            if (a.orderId != null) {
                // live order will fill; just log touch
                if (Boolean.TRUE.equals(verboseLog)) {
                    Log.info(String.format(Locale.US,
                            "LVN TOUCH %s entry=%.2f tape=%.2f order=%s %s",
                            a.side, a.entryPts, px, a.orderId, formatTime(nowNs)));
                }
                continue;
            }
            // Signal-only path: no order placed — emit fill signal for parity logs
            onSignalFill(a, px);
        }
    }

    private void maybeSnap(long tsNs) {
        if (!isEs()) {
            return; // confluence snap/trade only on ES
        }
        ZonedDateTime z = ukTime(tsNs);
        LocalDate day = z.toLocalDate();
        feedDateUk = day;
        if (snapDay != null && !snapDay.equals(day)) {
            // New calendar day — auto reset
            onResetDay();
        }
        LvnStrategyState.Settings s = LvnStrategyState.settings();
        int minOfDay = z.getHour() * 60 + z.getMinute();
        if (snapDoneForDay || minOfDay < s.snapUkMin) {
            return;
        }
        // Snap once we reach/pass 14:30
        LvnLevelFile.Levels lv = LvnStrategyState.esLevels();
        if (lv == null || !lv.hasAny()) {
            // Do not set snapDone — allow snap once LVNs are loaded, but only near 14:30
            if (Boolean.TRUE.equals(verboseLog) && z.getSecond() < 1
                    && (minOfDay == s.snapUkMin || minOfDay % 5 == 0)) {
                Log.info("LVN SNAP waiting — no ES LVN levels loaded (feed "
                        + String.format("%02d:%02d", z.getHour(), z.getMinute()) + ")");
            }
            return;
        }
        // Levels loaded after the open: current book is not the 14:30 snapshot.
        // Research trades need the 14:30 book — restart replay from before 14:30.
        int lateMins = minOfDay - s.snapUkMin;
        if (lateMins > 2) {
            if (Boolean.TRUE.equals(verboseLog)) {
                Log.info(String.format(Locale.US,
                        "LVN SNAP missed 14:30 (feed %02d:%02d, +%d min) — "
                                + "load ES LVNs, Reset day, replay through 14:30",
                        z.getHour(), z.getMinute(), lateMins));
            }
            snapDoneForDay = true; // stop retry spam; user must Reset day / rewind
            snapDay = day;
            publishLive("SNAP missed 14:30 — rewind replay");
            return;
        }
        // Refresh detector knobs from console before snap
        detector.config().offsetPts = s.offsetPts;
        detector.config().nearLvnPts = s.nearLvnPts;
        detector.config().minOpenDistPts = s.minOpenDistPts;
        detector.setLvns(lv.lvn2, lv.lvn3);

        Double mid = detector.midPts();
        double openMid = mid == null ? 0.0 : mid;
        int n = detector.snapshot(tsNs, openMid);
        snapDoneForDay = true;
        snapDay = day;
        cancelledAtEntryUntil = false;
        flattenedAtEos = false;

        Log.info(String.format(Locale.US,
                "LVN SNAP %s openMid=%.2f lvn2=%.2f lvn3=%.2f clusters=%d armed=%d rank=%s %s | %s",
                formatTime(tsNs), detector.openMidPts(), lv.lvn2, lv.lvn3,
                detector.snapClusters().size(), n, detector.rankingMode(), detector.summary(),
                detector.lastSnapDiag()));
        if (n == 0) {
            Log.info("LVN SNAP armed=0 — no bright cluster within nearLvn of LVN2/3 "
                    + "(check book sizes vs research; see diag above)");
        }
        for (ArmedLevel a : detector.armedLevels()) {
            Log.info(String.format(Locale.US,
                    "LVN ARM %s centre=%.2f level=%.2f(%s) entry=%.2f size=%d lvn=%s@%.2f dist=%.2f distOpen=%.2f",
                    a.side, a.centrePts, a.levelPx, a.levelSrc, a.entryPts, a.size,
                    a.lvnId, a.lvnPx, a.lvnDist, a.distOpen));
        }
        Log.info(String.format(Locale.US,
                "LVN SNAP sides BID=%d ASK=%d nextLong=%s nextShort=%s",
                detector.countTradeable(OpenClusterDetector.Side.BID),
                detector.countTradeable(OpenClusterDetector.Side.ASK),
                detector.nearestArmed(OpenClusterDetector.Side.BID, openMid) == null ? "—"
                        : String.format(Locale.US, "%.2f",
                        detector.nearestArmed(OpenClusterDetector.Side.BID, openMid).entryPts),
                detector.nearestArmed(OpenClusterDetector.Side.ASK, openMid) == null ? "—"
                        : String.format(Locale.US, "%.2f",
                        detector.nearestArmed(OpenClusterDetector.Side.ASK, openMid).entryPts)));
        // One resting entry per side: nearest long (BID) + nearest short (ASK) to open mid
        placeNearestEntryOrders("snap");
        publishLive("SNAP armed=" + n);
    }

    private void maybeSessionOps(long tsNs) {
        if (!detector.isSnapped()) {
            return;
        }
        ZonedDateTime z = ukTime(tsNs);
        int minOfDay = z.getHour() * 60 + z.getMinute();
        LvnStrategyState.Settings s = LvnStrategyState.settings();
        if (!cancelledAtEntryUntil && minOfDay >= s.entryUntilUkMin) {
            cancelledAtEntryUntil = true;
            Log.info("LVN entry-until " + minToHhmm(s.entryUntilUkMin) + " — cancel unfilled limits");
            cancelPendingLimits();
        }
        if (!flattenedAtEos && minOfDay >= s.eosUkMin) {
            flattenedAtEos = true;
            Log.info("LVN EOS " + minToHhmm(s.eosUkMin) + " — flatten if in position");
            if (flatCheckPosition() != 0) {
                doFlatten();
            }
        }
        // Kill switch
        double kill = s.killMaxSessionLossPts;
        if (kill > 0 && sessionNetPts <= -kill && !LvnStrategyState.isHalted()) {
            LvnStrategyState.setHalted(true, "kill sessionNet=" + sessionNetPts);
            cancelPendingLimits();
        }
    }

    // ------------------------------------------------------------ orders

    /** Open-plan mid (research parity); falls back to live BBO mid. */
    private double refMidPts() {
        if (detector.openMidPts() > 0) {
            return detector.openMidPts();
        }
        Double m = detector.midPts();
        return m == null ? 0.0 : m;
    }

    /** Next Long/Short for resting orders: nearest to open mid, not marketable vs live mid. */
    private ArmedLevel nearestForEntry(OpenClusterDetector.Side side) {
        double ref = refMidPts();
        if (ref <= 0) {
            return null;
        }
        return detector.nearestArmed(side, ref, detector.midPts());
    }

    /** Next Long/Short chart marks: nearest to open mid (always show armed short/long). */
    private ArmedLevel nearestForDisplay(OpenClusterDetector.Side side) {
        double ref = refMidPts();
        if (ref <= 0) {
            return null;
        }
        return detector.nearestArmed(side, ref, null);
    }

    /** Interval/realtime hook: place or cancel without spamming logs every 250ms. */
    private void ensureNearestEntryOrders(String via) {
        if (!isEs() || !detector.isSnapped()) {
            return;
        }
        if (!LvnStrategyState.tradingEnabled() || !realtimeStarted || api == null) {
            return;
        }
        if (inTrade()) {
            if (!pendingEntryOrderIds.isEmpty()) {
                Log.info("LVN ORDER in-position — cancel pending entries (" + via + ")");
                cancelPendingLimits();
            }
            return;
        }
        double ref = refMidPts();
        if (ref <= 0) {
            return;
        }
        ArmedLevel nearestLong = nearestForEntry(OpenClusterDetector.Side.BID);
        ArmedLevel nearestShort = nearestForEntry(OpenClusterDetector.Side.ASK);
        boolean needLong = nearestLong != null && LvnStrategyState.allowSide(1)
                && !hasWorkingEntry(nearestLong);
        boolean needShort = nearestShort != null && LvnStrategyState.allowSide(-1)
                && !hasWorkingEntry(nearestShort);
        boolean extras = hasNonNearestWorking(nearestLong, nearestShort);
        if (!needLong && !needShort && !extras) {
            return;
        }
        placeNearestEntryOrders(via, ref, nearestLong, nearestShort);
    }

    /**
     * True when we hold a position or an entry fill is latched.
     * Resting entry limits alone are <em>not</em> in-trade — both sides may work at once.
     */
    private boolean inTrade() {
        return flatCheckPosition() != 0 || openSide != 0;
    }

    /**
     * Position closed (TP/SL/flatten or exchange flat). Clear trade latch, retire the
     * filled level, and rest nearest Next Long / Next Short entry limits.
     */
    private void onBecameFlat(String via, Double exitPxOrNull) {
        int prevSide = openSide;
        double prevEntry = entryFillPx;
        openSide = 0;
        entryFillPx = 0;
        flatLocked = false;
        netFills = 0;
        position = 0;
        for (ArmedLevel a : detector.armedLevels()) {
            if (a.state == LevelState.FILLED) {
                detector.markDone(a);
            }
        }
        // Drop stale entry bindings so maybePlaceLimit will send again. Any leftover
        // working entry limits are cancelled first (should already be empty in-trade).
        cancelPendingLimits();
        clearTradeableOrderIds();
        double ref = refMidPts();
        ArmedLevel nextLong = nearestForEntry(OpenClusterDetector.Side.BID);
        ArmedLevel nextShort = nearestForEntry(OpenClusterDetector.Side.ASK);
        Log.info(String.format(Locale.US,
                "LVN FLAT via=%s was=%s entry=%.2f exit=%s tradeable BID=%d ASK=%d "
                        + "nextLong=%s nextShort=%s openMid=%.2f — placing",
                via,
                prevSide == 0 ? "—" : (prevSide > 0 ? "LONG" : "SHORT"),
                prevEntry,
                exitPxOrNull == null ? "—" : String.format(Locale.US, "%.2f", exitPxOrNull),
                detector.countTradeable(OpenClusterDetector.Side.BID),
                detector.countTradeable(OpenClusterDetector.Side.ASK),
                nextLong == null ? "—" : String.format(Locale.US, "%.2f", nextLong.entryPts),
                nextShort == null ? "—" : String.format(Locale.US, "%.2f", nextShort.entryPts),
                ref));
        publishLive("FLAT " + via);
        if (ref > 0) {
            placeNearestEntryOrders(via, ref, nextLong, nextShort);
        }
    }

    /** Clear orderId on every still-tradeable arm (ARMED / PULLED). */
    private void clearTradeableOrderIds() {
        for (ArmedLevel a : detector.armedLevels()) {
            if (a.isTradeable()) {
                a.orderId = null;
            }
        }
    }

    /**
     * When flat: at most one BID and one ASK resting entry — the nearest armed
     * level on each side to mid. When in a position: cancel any pending entries.
     */
    private void placeNearestEntryOrders(String via) {
        double ref = refMidPts();
        if (ref <= 0) {
            return;
        }
        placeNearestEntryOrders(via, ref,
                nearestForEntry(OpenClusterDetector.Side.BID),
                nearestForEntry(OpenClusterDetector.Side.ASK));
    }

    private void placeNearestEntryOrders(
            String via, double mid, ArmedLevel nearestLong, ArmedLevel nearestShort) {
        if (!isEs()) {
            return;
        }
        if (!LvnStrategyState.tradingEnabled() || !realtimeStarted || api == null) {
            return;
        }
        if (inTrade()) {
            if (!pendingEntryOrderIds.isEmpty()) {
                Log.info("LVN ORDER in-position — cancel pending entries (" + via + ")");
                cancelPendingLimits();
            }
            return;
        }
        cancelNonNearestEntries(nearestLong, nearestShort);
        if (nearestLong != null && LvnStrategyState.allowSide(1)) {
            maybePlaceLimit(nearestLong);
        }
        if (nearestShort != null && LvnStrategyState.allowSide(-1)) {
            maybePlaceLimit(nearestShort);
        }
        Log.info(String.format(Locale.US,
                "LVN ORDER nearest mid=%.2f long=%s short=%s (%s)",
                mid,
                nearestLong == null ? "—" : String.format(Locale.US, "%.2f", nearestLong.entryPts),
                nearestShort == null ? "—" : String.format(Locale.US, "%.2f", nearestShort.entryPts),
                via));
    }

    private boolean hasWorkingEntry(ArmedLevel a) {
        if (a == null || a.orderId == null) {
            return false;
        }
        if (a.orderId.startsWith("pending:")) {
            return true;
        }
        return pendingEntryOrderIds.contains(a.orderId);
    }

    private boolean hasNonNearestWorking(ArmedLevel keepLong, ArmedLevel keepShort) {
        double tol = Math.max(1e-6, pips * 0.5);
        for (ArmedLevel a : detector.armedLevels()) {
            if (!a.isTradeable() || !hasWorkingEntry(a)) {
                continue;
            }
            boolean keep = (keepLong != null && (a == keepLong
                    || (a.side == keepLong.side && Math.abs(a.entryPts - keepLong.entryPts) <= tol)))
                    || (keepShort != null && (a == keepShort
                    || (a.side == keepShort.side && Math.abs(a.entryPts - keepShort.entryPts) <= tol)));
            if (!keep) {
                return true;
            }
        }
        return false;
    }

    private void cancelNonNearestEntries(ArmedLevel keepLong, ArmedLevel keepShort) {
        double tol = Math.max(1e-6, pips * 0.5);
        for (ArmedLevel a : detector.armedLevels()) {
            if (!a.isTradeable() || a.orderId == null) {
                continue;
            }
            boolean keep = (keepLong != null && a == keepLong)
                    || (keepShort != null && a == keepShort)
                    || (keepLong != null && a.side == keepLong.side
                        && Math.abs(a.entryPts - keepLong.entryPts) <= tol)
                    || (keepShort != null && a.side == keepShort.side
                        && Math.abs(a.entryPts - keepShort.entryPts) <= tol);
            if (keep) {
                continue;
            }
            try {
                if (!a.orderId.startsWith("pending:")) {
                    api.updateOrder(new OrderCancelParameters(a.orderId));
                    pendingEntryOrderIds.remove(a.orderId);
                }
                Log.info(String.format(Locale.US,
                        "LVN CANCEL non-nearest %s entry=%.2f id=%s", a.side, a.entryPts, a.orderId));
                a.orderId = null;
            } catch (Throwable t) {
                Log.info("LVN CANCEL non-nearest failed: " + t);
            }
        }
    }

    private void maybePlaceLimit(ArmedLevel a) {
        if (!LvnStrategyState.tradingEnabled()) {
            return;
        }
        if (!realtimeStarted || api == null) {
            return;
        }
        if (inTrade()) {
            return;
        }
        if (!LvnStrategyState.allowSide(a.tradeSide())) {
            Log.info("LVN ORDER skip side disabled " + a.side);
            return;
        }
        if (hasWorkingEntry(a)) {
            return;
        }
        // Stale id (cancelled / unbound) — allow re-send
        a.orderId = null;
        // One order per side max
        double tol = Math.max(1e-6, pips * 0.5);
        for (ArmedLevel o : detector.armedLevels()) {
            if (o == a || !hasWorkingEntry(o)) {
                continue;
            }
            if (o.side == a.side) {
                Log.info(String.format(Locale.US,
                        "LVN ORDER skip — already have %s entry at %.2f", o.side, o.entryPts));
                return;
            }
            if (Math.abs(o.entryPts - a.entryPts) <= tol) {
                a.orderId = o.orderId;
                return;
            }
        }
        sendLimitBracket(a);
    }

    private void sendLimitBracket(ArmedLevel a) {
        LvnStrategyState.Settings s = LvnStrategyState.settings();
        int size = Math.max(1, s.orderSize);
        boolean isBuy = a.side == OpenClusterDetector.Side.BID;
        double pip = pips > 0 ? pips : 0.25;
        int tpTicks = s.tpPts > 0 ? Math.max(1, (int) Math.round(s.tpPts / pip)) : 0;
        int slTicks = s.slPts > 0 ? Math.max(1, (int) Math.round(s.slPts / pip)) : 0;
        refreshTradingAccountId("order");
        try {
            // limitPrice = entry; stopPrice = NaN; TP/SL tick offsets from fill
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias, isBuy, size, OrderDuration.GTC, null,
                    a.entryPts, Double.NaN, tpTicks, slTicks, 0, 0, false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            SimpleOrderSendParameters order = builder.build();
            api.sendOrder(order);
            // order id assigned asynchronously — track by entry until update
            a.orderId = "pending:" + a.side + ":" + a.entryPts;
            Log.info(String.format(Locale.US,
                    "LVN ORDER sent %s size=%d limit=%.2f tp=%d ticks (~%.1f) sl=%d ticks (~%.1f) "
                            + "level=%.2f centre=%.2f account=%s %s",
                    isBuy ? "BUY" : "SELL", size, a.entryPts, tpTicks, s.tpPts, slTicks, s.slPts,
                    a.levelPx, a.centrePts,
                    tradingAccountId == null ? "null" : tradingAccountId, formatTime(nowNs)));
        } catch (Throwable t) {
            Log.info("LVN ORDER failed: " + t);
        }
    }

    private void onSignalFill(ArmedLevel a, double px) {
        if (!a.isTradeable()) {
            return;
        }
        // Never consume a next level while holding a position (re-rested on flat)
        if (inTrade()) {
            return;
        }
        if (!LvnStrategyState.allowSide(a.tradeSide())) {
            return;
        }
        // Check entry-until
        ZonedDateTime z = ukTime(nowNs);
        int minOfDay = z.getHour() * 60 + z.getMinute();
        if (minOfDay >= LvnStrategyState.settings().entryUntilUkMin) {
            return;
        }
        detector.markFilled(a, nowNs);
        signalThisInterval = true;
        signalIndicator.addPoint(a.tradeSide());
        if (a.tradeSide() > 0) {
            longMarker.addPoint(toPrimaryAxis(a.entryPts));
            shortMarker.addPoint(Double.NaN);
        } else {
            shortMarker.addPoint(toPrimaryAxis(a.entryPts));
            longMarker.addPoint(Double.NaN);
        }
        Log.info(String.format(Locale.US,
                "LVN FILL %s entry=%.2f tape=%.2f level=%.2f(%s) centre=%.2f lvn=%s@%.2f "
                        + "trading=%s realtime=%s %s",
                a.side, a.entryPts, px, a.levelPx, a.levelSrc, a.centrePts,
                a.lvnId, a.lvnPx, LvnStrategyState.tradingEnabled(), realtimeStarted,
                formatTime(nowNs)));
        // If trading on and no order yet, place limit at entry
        if (LvnStrategyState.tradingEnabled() && realtimeStarted && a.orderId == null) {
            if (flatCheckPosition() != 0 || flatLocked) {
                Log.info("LVN FILL skip order — not flat");
            } else {
                sendLimitBracket(a);
            }
        }
        publishLive("FILL " + a.side + "@" + a.entryPts);
    }

    private void doFlatten() {
        int pos = flatCheckPosition();
        cancelPendingLimits();
        if (pos == 0 || api == null || primaryAlias == null) {
            Log.info("LVN FLATTEN: nothing to close (pos=" + pos + ")");
            return;
        }
        boolean isBuy = pos < 0;
        int size = Math.abs(pos);
        refreshTradingAccountId("flatten");
        flatLocked = true;
        try {
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias, isBuy, size, OrderDuration.GTC, null,
                    Double.NaN, Double.NaN, 0, 0, 0, 0, false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            api.sendOrder(builder.build());
            Log.info(String.format("LVN FLATTEN sent %s size=%d (close pos=%d)",
                    isBuy ? "BUY" : "SELL", size, pos));
        } catch (Throwable t) {
            Log.info("LVN FLATTEN failed: " + t);
            flatLocked = false;
        }
        // Only retire the filled trade level — leave other ARMED levels for re-entry
        // once flat (onBecameFlat / pos update will rest Next Long / Next Short).
        for (ArmedLevel a : detector.armedLevels()) {
            if (a.state == LevelState.FILLED) {
                detector.markDone(a);
            }
        }
    }

    private void cancelPendingLimits() {
        if (api == null) {
            return;
        }
        Set<String> ids = new HashSet<>(pendingEntryOrderIds);
        for (String id : ids) {
            try {
                api.updateOrder(new OrderCancelParameters(id));
                Log.info("LVN CANCEL order=" + id);
            } catch (Throwable t) {
                Log.info("LVN CANCEL failed id=" + id + ": " + t);
            }
        }
        pendingEntryOrderIds.clear();
        clearTradeableOrderIds();
    }

    // ------------------------------------------------------------ interval / UI

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_250_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        if (!signalThisInterval) {
            signalIndicator.addPoint(0);
        }
        signalThisInterval = false;

        applyLevelsToDetector();
        plotLvnLines();
        plotHvnLines();

        // Next-trade highlights: nearest ARMED long below / short above mid
        // (shown whether flat or in a position — orders only when flat)
        plotNextTradeMarks();

        if (isEs() && detector.isSnapped()) {
            detector.refreshPullDisplay();
            ensureNearestEntryOrders("interval");
        }

        // Live large-order table: ES preferred; NQ fills the console when no ES attach
        if (isEs() || isNq()) {
            publishLive(null);
        }
    }

    /** Highlight Next Long / Next Short from the open plan (visible while in a trade too). */
    private void plotNextTradeMarks() {
        double bidMark = Double.NaN;
        double askMark = Double.NaN;
        if (isEs() && detector.isSnapped() && refMidPts() > 0) {
            ArmedLevel nearestLong = nearestForDisplay(OpenClusterDetector.Side.BID);
            ArmedLevel nearestShort = nearestForDisplay(OpenClusterDetector.Side.ASK);
            if (nearestLong != null) {
                bidMark = toPrimaryAxis(nearestLong.entryPts);
            }
            if (nearestShort != null) {
                askMark = toPrimaryAxis(nearestShort.entryPts);
            }
        }
        // Always refresh both — do not let a signal flash clear the opposite side permanently
        longMarker.addPoint(bidMark);
        shortMarker.addPoint(askMark);
    }

    /**
     * Bookmap PRIMARY indicators on this install use <em>tick indices</em> as the
     * Y value (meter shows {@code value * pips}). Feeding chart points (7780)
     * drew lines at ~1945 — off the visible heatmap.
     */
    private double toPrimaryAxis(double chartPts) {
        if (Double.isNaN(chartPts)) {
            return Double.NaN;
        }
        return pips > 0 ? chartPts / pips : chartPts;
    }

    private static void styleLevelLine(Indicator ind, Color color, int priority) {
        ind.setColor(color);
        ind.setWidth(3);
        ind.setRenderPriority(priority);
    }

    /** Draw this chart's market LVNs when console toggle is on. */
    private void plotLvnLines() {
        LvnLevelFile.Levels lv = LvnStrategyState.levelsFor(instrumentRole);
        if (!LvnStrategyState.showLvnsOnChart() || lv == null || !lv.hasAny()) {
            hideLine(lvn2Marker);
            hideLine(lvn3Marker);
            return;
        }
        lvn2IconPx = plotLevel(lvn2Marker, lvn2IconPx, lv.lvn2, COL_LVN);
        lvn3IconPx = plotLevel(lvn3Marker, lvn3IconPx, lv.lvn3, COL_LVN);
        if (Boolean.TRUE.equals(verboseLog) && !loggedLvnPlot) {
            loggedLvnPlot = true;
            Log.info(String.format(Locale.US,
                    "LVN chart lines %s at %.2f / %.2f (price tags via icon)",
                    instrumentRole, lv.lvn2, lv.lvn3));
        }
    }

    /** Draw HVN1/2/3 from the same export as-of row when console toggle is on. */
    private void plotHvnLines() {
        LvnLevelFile.Levels lv = LvnStrategyState.levelsFor(instrumentRole);
        if (!LvnStrategyState.showHvnsOnChart() || lv == null || !lv.hasAnyHvn()) {
            hideLine(hvn1Marker);
            hideLine(hvn2Marker);
            hideLine(hvn3Marker);
            return;
        }
        hvn1IconPx = plotLevel(hvn1Marker, hvn1IconPx, lv.hvn1, COL_HVN);
        hvn2IconPx = plotLevel(hvn2Marker, hvn2IconPx, lv.hvn2, COL_HVN);
        hvn3IconPx = plotLevel(hvn3Marker, hvn3IconPx, lv.hvn3, COL_HVN);
    }

    private static void hideLine(Indicator ind) {
        if (ind != null) {
            ind.addPoint(Double.NaN);
        }
    }

    /**
     * Plot a level line (tick-axis) and stamp a price text icon when the price is new.
     * {@code registerIndicator} is never called here — only {@link Indicator#addPoint}
     * / {@link Indicator#addIcon}, which are allowed after initialize.
     *
     * @return updated icon price tracker
     */
    private double plotLevel(Indicator ind, double iconPx, double chartPts, Color color) {
        if (ind == null) {
            return iconPx;
        }
        if (chartPts == 0.0) {
            ind.addPoint(Double.NaN);
            return iconPx;
        }
        double axis = toPrimaryAxis(chartPts);
        ind.addPoint(axis);
        if (Double.isNaN(iconPx) || Math.abs(iconPx - chartPts) > 1e-6) {
            String label = formatPriceLabel(chartPts);
            BufferedImage icon = renderPriceIcon(label, color);
            ind.addIcon(axis, icon, 4, icon.getHeight() / 2);
            return chartPts;
        }
        return iconPx;
    }

    private static String formatPriceLabel(double chartPts) {
        if (Math.abs(chartPts - Math.rint(chartPts)) < 1e-9) {
            return String.format(Locale.US, "%.0f", chartPts);
        }
        return String.format(Locale.US, "%.2f", chartPts);
    }

    private static BufferedImage renderPriceIcon(String text, Color color) {
        Font font = new Font("SansSerif", Font.BOLD, 12);
        BufferedImage probe = new BufferedImage(1, 1, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g0 = probe.createGraphics();
        g0.setFont(font);
        FontMetrics fm = g0.getFontMetrics();
        int w = fm.stringWidth(text) + 8;
        int h = fm.getHeight() + 4;
        g0.dispose();
        BufferedImage img = new BufferedImage(w, h, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g = img.createGraphics();
        g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
        g.setFont(font);
        g.setColor(new Color(0, 0, 0, 160));
        g.fillRoundRect(0, 0, w, h, 6, 6);
        g.setColor(color);
        g.drawString(text, 4, fm.getAscent() + 1);
        g.dispose();
        return img;
    }

    private void publishLive(String event) {
        if (!isEs() && !isNq()) {
            return;
        }
        Double mid = detector.midPts();
        List<OpenClusterDetector.DisplayRow> rows = detector.displayRows(5);
        if (Boolean.TRUE.equals(verboseLog) && event == null && nowNs > 0
                && nowNs - lastBookLogNs >= 15_000_000_000L) {
            lastBookLogNs = nowNs;
            LvnLevelFile.Levels lv = LvnStrategyState.levelsFor(instrumentRole);
            Log.info(String.format(Locale.US,
                    "LVN BOOK %s bids=%d asks=%d mid=%s rows=%d showLvn=%s lvns=%s",
                    instrumentRole, detector.bidLevels(), detector.askLevels(),
                    mid == null ? "—" : String.format(Locale.US, "%.2f", mid),
                    rows.size(),
                    LvnStrategyState.showLvnsOnChart(),
                    lv != null
                            ? String.format(Locale.US, "2=%.2f 3=%.2f", lv.lvn2, lv.lvn3)
                            : "none"));
        }
        LvnStrategyState.publishLive(
                instrumentRole, primaryAlias,
                rows, mid == null ? 0.0 : mid, flatCheckPosition(),
                detector.isSnapped(), event);
    }

    // ------------------------------------------------------------ position / fills

    private int flatCheckPosition() {
        return position != 0 ? position : netFills;
    }

    @Override
    public void onPositionUpdate(StatusInfo statusInfo) {
        if (statusInfo == null) {
            return;
        }
        if (tradingAccountId != null && statusInfo.accountId != null
                && !tradingAccountId.equals(statusInfo.accountId)) {
            return;
        }
        int prevPos = position;
        position = statusInfo.position;
        // Resting entry limits (workingBuys/Sells) must NOT lock — we need both
        // a long and a short limit working at once while flat. Only a real
        // position locks. When we go flat, re-arm Next Long / Next Short.
        if (statusInfo.position != 0) {
            flatLocked = true;
            netFills = statusInfo.position;
            publishLive(null);
            return;
        }
        boolean stillLatched = openSide != 0;
        boolean justFlattened = prevPos != 0;
        if (stillLatched || justFlattened) {
            // Exchange flat — covers exit-fill races (stale position blocked re-arm)
            // and missed bracket callbacks. Idempotent if exit path already re-armed.
            onBecameFlat(stillLatched ? "pos-flat" : "pos-flat-confirm", detector.midPts());
        } else {
            flatLocked = false;
            netFills = 0;
            publishLive(null);
        }
    }

    @Override
    public void onOrderUpdated(OrderInfoUpdate order) {
        if (order == null || order.orderId == null) {
            return;
        }
        accountListManager.onOrderUpdated(order);
        if (primaryAlias != null && order.instrumentAlias != null
                && !primaryAlias.equals(order.instrumentAlias)) {
            return;
        }
        if (tradingAccountId != null && order.accountId != null
                && !tradingAccountId.equals(order.accountId)) {
            return;
        }

        // Bind pending entry limits to tradeable levels by limit price
        if (order.type == OrderType.LMT && order.limitPrice > 0 && !orderToLevel.containsKey(order.orderId)) {
            for (ArmedLevel a : detector.armedLevels()) {
                if (a.isTradeable()
                        && Math.abs(a.entryPts - order.limitPrice) < 0.02) {
                    a.orderId = order.orderId;
                    orderToLevel.put(order.orderId, a);
                    pendingEntryOrderIds.add(order.orderId);
                    Log.info("LVN ORDER bound id=" + order.orderId + " entry=" + a.entryPts);
                    break;
                }
            }
        }

        int prevFilled = filledByOrderId.getOrDefault(order.orderId, 0);
        int filled = order.filled;
        if (filled <= prevFilled) {
            filledByOrderId.put(order.orderId, Math.max(prevFilled, filled));
            return;
        }
        int delta = filled - prevFilled;
        filledByOrderId.put(order.orderId, filled);
        netFills += order.isBuy ? delta : -delta;
        flatLocked = !(netFills == 0 && position == 0);

        ArmedLevel linked = orderToLevel.get(order.orderId);
        boolean entryFill = linked != null && linked.isTradeable();
        if (entryFill) {
            detector.markFilled(linked, nowNs);
            pendingEntryOrderIds.remove(order.orderId);
            openSide = order.isBuy ? 1 : -1;
            entryFillPx = order.limitPrice > 0 ? order.limitPrice : linked.entryPts;
            flatLocked = true;
            // In a trade: cancel all other entry limits (no new trades until flat)
            cancelSiblingEntries(order.orderId);
            cancelPendingLimits(); // belt-and-suspenders — clear any leftover pendings
            // Re-bind filled level's orderId so cancelPending didn't clear the working bracket
            linked.orderId = order.orderId;
            signalThisInterval = true;
            signalIndicator.addPoint(openSide);
            Log.info(String.format(Locale.US,
                    "LVN FILL order=%s %s entry=%.2f delta=%d level=%.2f %s",
                    order.orderId, order.isBuy ? "BUY" : "SELL", entryFillPx, delta,
                    linked.levelPx, formatTime(nowNs)));
            publishLive("FILL order " + order.orderId);
            return;
        }

        // Bracket exit fill (TP / SL / flatten) — opposite side to openSide, not an entry level
        if (openSide != 0 && linked == null) {
            boolean exitDir = (order.isBuy && openSide < 0) || (!order.isBuy && openSide > 0);
            if (exitDir) {
                double exitPx = order.averageFillPrice;
                if (Double.isNaN(exitPx) || exitPx <= 0) {
                    exitPx = order.limitPrice > 0 ? order.limitPrice
                            : (order.stopPrice > 0 ? order.stopPrice : entryFillPx);
                }
                double pnl = openSide > 0 ? (exitPx - entryFillPx) : (entryFillPx - exitPx);
                sessionNetPts += pnl;
                sessionTrades++;
                Log.info(String.format(Locale.US,
                        "LVN TRADE CLOSED pnl=%.2f entry=%.2f exit=%.2f type=%s sessionNet=%.2f n=%d %s",
                        pnl, entryFillPx, exitPx, order.type, sessionNetPts, sessionTrades,
                        formatTime(nowNs)));
                // Clears latch (incl. stale position) and rests Next Long / Next Short
                onBecameFlat("exit-" + order.type, exitPx);
            }
        }
    }

    private void cancelSiblingEntries(String keepId) {
        Set<String> copy = new HashSet<>(pendingEntryOrderIds);
        for (String id : copy) {
            if (id.equals(keepId)) {
                continue;
            }
            try {
                api.updateOrder(new OrderCancelParameters(id));
                pendingEntryOrderIds.remove(id);
                Log.info("LVN CANCEL sibling entry " + id);
            } catch (Throwable t) {
                Log.info("LVN CANCEL sibling failed: " + t);
            }
        }
    }

    @Override
    public void onOrderExecuted(velox.api.layer1.data.ExecutionInfo executionInfo) {
        // Entry refinement when averageFillPrice is missing (replay SIM).
        if (executionInfo == null || openSide == 0) {
            return;
        }
        double px = executionInfo.price;
        if (Double.isNaN(px) || px <= 0) {
            return;
        }
        if (!(entryFillPx > 0) || Double.isNaN(entryFillPx)) {
            entryFillPx = px >= 80_000.0 ? px * pips : px;
            Log.info(String.format(Locale.US, "LVN entry from execution px=%.2f", entryFillPx));
        }
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        Log.info("LVN realtime started — orders enabled when console trading ON");
        if (detector.isSnapped() && LvnStrategyState.tradingEnabled()) {
            placeNearestEntryOrders("realtime");
        }
    }

    private void installAccountListener(Api api) {
        try {
            provider = api.getProvider();
            if (provider == null) {
                Log.info("LVN account listener: provider null");
                return;
            }
            adminListener = new Layer1ApiAdminAdapter() {
                @Override
                public void onUserMessage(Object data) {
                    boolean updated = accountListManager.onUserMessage(data);
                    if (data instanceof SelectedAccountChangedMessage || updated) {
                        refreshTradingAccountId("accountMsg");
                    }
                }
            };
            provider.addListener(adminListener);
        } catch (Throwable t) {
            Log.info("LVN account listener not attached: " + t.getMessage());
        }
    }

    private void refreshTradingAccountId(String via) {
        if (!Boolean.TRUE.equals(useSelectedAccount) || primaryAlias == null) {
            tradingAccountId = null;
            return;
        }
        try {
            var selected = accountListManager.getSelectedAccountIdForAlias(primaryAlias);
            if (selected.isPresent()) {
                String id = selected.get();
                tradingAccountId = (id == null || id.isEmpty()) ? null : id;
                return;
            }
            Set<AccountInfo> forAlias = accountListManager.getAccountsByAlias(primaryAlias);
            if (forAlias != null && forAlias.size() == 1) {
                tradingAccountId = forAlias.iterator().next().id;
            }
        } catch (Throwable t) {
            // keep previous
        }
    }

    @Override
    public void stop() {
        if (isEs()) {
            LvnStrategyState.unregisterFlattener(flattener);
            LvnStrategyState.unregisterCancelPending(canceler);
            LvnStrategyState.unregisterResetDay(resetDayHook);
            cancelPendingLimits();
        }
        if (isEs() || isNq()) {
            LvnStrategyState.unregisterAttach(instrumentRole);
        }
        if (provider != null && adminListener != null) {
            try {
                provider.removeListener(adminListener);
            } catch (Throwable ignored) {
                // ignore
            }
        }
        Log.info("LVN stop alias=" + primaryAlias + " role=" + instrumentRole);
    }

    // ------------------------------------------------------------ time helpers

    private static ZonedDateTime ukTime(long tsNs) {
        return java.time.Instant.ofEpochSecond(tsNs / 1_000_000_000L, tsNs % 1_000_000_000L)
                .atZone(UK);
    }

    private static String formatTime(long tsNs) {
        if (tsNs <= 0) {
            return "?";
        }
        return TIME_FMT.format(ukTime(tsNs));
    }

    private static String minToHhmm(int minOfDay) {
        return String.format(Locale.US, "%02d:%02d",
                Math.floorDiv(minOfDay, 60), Math.floorMod(minOfDay, 60));
    }
}
