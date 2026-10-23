package com.bookmap.orderflow.trade;

import java.awt.Color;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;

import com.bookmap.orderflow.core.CumulativeDeltaRange;
import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.TapeBurstTracker;
import com.bookmap.orderflow.core.TapeBurstTracker.Burst;

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
import velox.api.layer1.data.OrderDuration;
import velox.api.layer1.data.OrderEntryType;
import velox.api.layer1.data.OrderInfoUpdate;
import velox.api.layer1.data.OrderMoveParameters;
import velox.api.layer1.data.OrderStatus;
import velox.api.layer1.data.OrderType;
import velox.api.layer1.data.SimpleOrderSendParameters;
import velox.api.layer1.data.SimpleOrderSendParametersBuilder;
import velox.api.layer1.data.StatusInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.messages.SelectedAccountChangedMessage;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyIndicator.GraphType;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.AxisGroup;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.HistoricalModeListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.MultiInstrumentListener;
import velox.api.layer1.simplified.OrdersListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.PositionListener;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Auto-trade / replay signal for the strongest research lead: EXP-0008
 * ES+NQ fast-tape momentum (optional CD pullback-to-MA gate from EXP-0009b).
 *
 * Attach on NQ with ES also open. Default: signals only (enableTrading=false).
 * On Replay, leave trading off first and verify markers/logs; enable only in
 * simulation / paper when ready.
 *
 * {@link Layer1TradingStrategy} is required before {@code api.sendOrder}; without
 * it Bookmap throws AssertionError and the Global Exception Handler dialog.
 * That annotation also blocks use on non-demo live accounts.
 *
 * Uses {@link HistoricalModeListener} (not bare HistoricalDataListener) so we can
 * warm detectors on catchup but only send orders after {@link #onRealtimeStart()}.
 * Catchup orders are not meaningful in Replay — Bookmap fills them all at the
 * current cursor time.
 *
 * Side filter: Allow longs/shorts params seed {@link MatSideFilter}. Runtime
 * toggles: Ctrl+Shift+L/K/B/X via {@link MatSideHotkeys} (works with Simplified).
 *
 * Orders use the account selected in Bookmap's Account dropdown for this alias
 * (Simulator on Replay is fine). Null accountId is used only when none is known.
 *
 * Signal: +1 long / -1 short on bottom panel; PRIMARY markers at entry price.
 */
@Layer1SimpleAttachable
@Layer1TradingStrategy
@Layer1MultiAccountTradingSupported
@Layer1StrategyName("Momentum AutoTrade (ES+NQ)")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class MomentumAutoTradeModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        PositionListener,
        OrdersListener,
        HistoricalModeListener {

    // --- EXP-0008 tape defaults ---
    @Parameter(name = "Bar ms", step = 50.0)
    Double barMs = 100.0;

    @Parameter(name = "Min trades / bar", step = 1.0)
    Double minTrades = 6.0;

    @Parameter(name = "Min volume / bar", step = 1.0)
    Double minVol = 25.0;

    @Parameter(name = "Min |imbalance|", step = 0.05)
    Double minImbalance = 0.55;

    @Parameter(name = "Min progress (price pts)", step = 0.25)
    Double minProgressPts = 0.25;

    @Parameter(name = "ES sync window ms", step = 50.0)
    Double syncMs = 1500.0;

    /** Offline dig (EXP-0008): lag&lt;250ms lifts path and hit rate vs lag≥250. */
    @Parameter(name = "Max ES lag ms (0=off)", step = 50.0)
    Double maxEsLagMs = 250.0;

    @Parameter(name = "Burst cooldown bars", step = 1.0)
    Double cooldownBars = 3.0;

    // --- CD pullback gate (EXP-0009b) ---
    @Parameter(name = "Require CD pullback to MA")
    Boolean requireCdPullback = false;

    @Parameter(name = "CD range ticks", step = 1.0)
    Double cdRangeTicks = 22.0;

    @Parameter(name = "CD SMA length", step = 1.0)
    Double cdSmaLen = 100.0;

    // --- Trading ---
    @Parameter(name = "Enable trading (OFF for signal-only)")
    Boolean enableTrading = false;

    /**
     * Seeds {@link MatSideFilter} on attach / when changed in the panel.
     * While trading prefer Ctrl+Shift hotkeys (see {@link MatSideHotkeys}).
     */
    @Parameter(name = "Allow longs")
    Boolean allowLongs = true;

    @Parameter(name = "Allow shorts")
    Boolean allowShorts = true;

    /** Ctrl+Shift+L/K toggle, Ctrl+Shift+B both, Ctrl+Shift+X none. */
    @Parameter(name = "Enable Ctrl+Shift side hotkeys")
    Boolean enableSideHotkeys = true;

    /** Last panel values pushed into {@link MatSideFilter} (detect UI changes). */
    private Boolean lastSyncedAllowLongs;
    private Boolean lastSyncedAllowShorts;
    private boolean sideHotkeysInstalled;
    private final MatTradeControl.Flattener flattenAction = this::flattenPosition;

    /**
     * When true, orders use Bookmap's Account dropdown for this instrument
     * (Simulator on Replay). When false, accountId is left null (provider primary).
     */
    @Parameter(name = "Use selected account (dropdown)")
    Boolean useSelectedAccount = true;

    @Parameter(name = "Order size", step = 1.0)
    Double orderSize = 1.0;

    /** When true, entry is sent with Bookmap bracket TP/SL (tick offsets). */
    @Parameter(name = "Attach brackets (TP/SL)")
    Boolean useBrackets = true;

    @Parameter(name = "Take profit (price pts)", step = 1.0)
    Double takeProfitPts = 10.0;

    @Parameter(name = "Stop loss (price pts)", step = 1.0)
    Double stopLossPts = 10.0;

    @Parameter(name = "Signal cooldown sec", step = 1.0)
    Double signalCooldownSec = 5.0;

    @Parameter(name = "Flat-only (no pyramid)")
    Boolean flatOnly = true;

    /** When stop fills, enter opposite side (respects Allow longs/shorts). */
    @Parameter(name = "Reverse on stop")
    Boolean reverseOnStop = false;

    /** If Reverse on stop: only reverse when opposite entry conditions pass. */
    @Parameter(name = "Reverse only if entry OK")
    Boolean reverseOnlyIfEntryOk = false;

    /** Near TP, push TP further by Take profit pts if same-side entry still OK. */
    @Parameter(name = "Extend TP if still favourable")
    Boolean extendTpIfFavourable = false;

    @Parameter(name = "TP extend proximity (pts)", step = 0.25)
    Double tpExtendProximityPts = 2.0;

    /** 0 = unlimited extends per trade. */
    @Parameter(name = "Max TP extends (0=unlimited)", step = 1.0)
    Double maxTpExtends = 3.0;

    private Api api;
    private Layer1ApiProvider provider;
    private Layer1ApiAdminAdapter adminListener;
    private final AccountListManager accountListManager = new AccountListManager();
    private String primaryAlias;
    private String currentAlias;
    private long nowNs;
    private double pips = 0.25;
    /** Last StatusInfo.position (may lag or miss in Replay SIM). */
    private int position;
    /** Net filled qty from OrdersListener (entry +, exit −). */
    private int netFills;
    /** True after entry send until flat again — blocks pyramid even if pos still 0. */
    private boolean flatLocked;
    private final Map<String, Integer> filledByOrderId = new HashMap<>();
    /** Last resolved trading account id (null = primary / unspecified). */
    private volatile String tradingAccountId;

    private final Map<String, InstrumentState> byAlias = new HashMap<>();

    private Indicator signalIndicator;
    private Indicator longMarker;
    private Indicator shortMarker;
    private Indicator esLagIndicator;
    private Indicator cdTrendIndicator;

    private long lastSignalNs;
    private int lastSignalSide;
    private boolean signalThisInterval;

    /** False during catchup/history; true after {@link #onRealtimeStart()}. */
    private volatile boolean realtimeStarted;

    /** Open trade: 0 flat, +1 long, −1 short. */
    private int openSide;
    private double entryFillPx;
    private String tpOrderId;
    private String slOrderId;
    private double currentTpPx;
    private int tpExtendCount;
    /** False after an extend until price backs out of the proximity band. */
    private boolean tpExtendArmed = true;
    /** One-shot reverse after SL fill (processed when flat). */
    private boolean pendingReverse;
    private int pendingReverseSide;

    private long lastSeenTsNs = -1;
    private long nqTradeCount;
    private long esTradeCount;
    private long nqBurstCount;
    private long esBurstCount;
    private long rejectLogNs;
    private static final long REJECT_LOG_INTERVAL_NS = 2_000_000_000L; // 2s replay-time
    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final DateTimeFormatter TIME_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS z");

    private static final class InstrumentState {
        String role = "OTHER";
        TapeBurstTracker burst;
        CumulativeDeltaRange cd;
        double lastPrice;
        double instrumentPips = 0.25;
    }

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.api = api;
        this.primaryAlias = alias;
        this.currentAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        this.realtimeStarted = false;
        this.position = 0;
        this.netFills = 0;
        this.flatLocked = false;
        this.filledByOrderId.clear();
        this.tradingAccountId = null;
        clearTradeContext("init");

        InstrumentState primary = newState(InstrumentClassifier.classify(info));
        byAlias.put(alias, primary);

        signalIndicator = api.registerIndicator("MAT Signal", GraphType.BOTTOM);
        esLagIndicator = api.registerIndicator("MAT ES lag ms", GraphType.BOTTOM);
        cdTrendIndicator = api.registerIndicator("MAT CD trend", GraphType.BOTTOM);
        longMarker = api.registerIndicator("MAT Long", GraphType.PRIMARY);
        shortMarker = api.registerIndicator("MAT Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 220, 60));
        esLagIndicator.setColor(new Color(160, 160, 160));
        cdTrendIndicator.setColor(new Color(120, 200, 255));
        longMarker.setColor(new Color(0, 200, 90));
        shortMarker.setColor(new Color(220, 60, 60));
        longMarker.setWidth(3);
        shortMarker.setWidth(3);

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(esLagIndicator);
        bottom.add(cdTrendIndicator);

        installAccountListener(api);
        refreshTradingAccountId("init");
        syncSideFilterFromParams("init");
        sideHotkeysInstalled = false;
        if (Boolean.TRUE.equals(enableSideHotkeys)) {
            MatSideHotkeys.install();
            sideHotkeysInstalled = true;
        }
        MatTradeControl.register(flattenAction);

        Log.info("Momentum AutoTrade init on " + alias
                + " role=" + primary.role
                + " trading=" + Boolean.TRUE.equals(enableTrading)
                + " longs=" + MatSideFilter.allowLongs()
                + " shorts=" + MatSideFilter.allowShorts()
                + " sideHotkeys=" + sideHotkeysInstalled
                + " useSelectedAccount=" + Boolean.TRUE.equals(useSelectedAccount)
                + " accountId=" + (tradingAccountId == null ? "null(primary)" : tradingAccountId)
                + " cdPullback=" + Boolean.TRUE.equals(requireCdPullback)
                + " maxEsLagMs=" + maxEsLagMs
                + " syncMs=" + syncMs
                + " pips=" + pips
                + " (trade price→chart via PriceTicks.toChartPoints)");
    }

    private void installAccountListener(Api api) {
        try {
            provider = api.getProvider();
            if (provider == null) {
                Log.info("MAT account listener: provider null — dropdown account not tracked");
                return;
            }
            adminListener = new Layer1ApiAdminAdapter() {
                @Override
                public void onUserMessage(Object data) {
                    boolean updated = accountListManager.onUserMessage(data);
                    if (data instanceof SelectedAccountChangedMessage) {
                        SelectedAccountChangedMessage msg = (SelectedAccountChangedMessage) data;
                        Log.info("MAT account selection changed alias=" + msg.alias
                                + " accountId=" + msg.accountId);
                        if (primaryAlias != null && primaryAlias.equals(msg.alias)) {
                            refreshTradingAccountId("selection");
                        }
                    } else if (updated) {
                        refreshTradingAccountId("accounts");
                    }
                }
            };
            provider.addListener(adminListener);
        } catch (Throwable t) {
            Log.info("MAT account listener install failed: " + t);
        }
    }

    private void refreshTradingAccountId(String reason) {
        String resolved = resolveAccountId();
        boolean unchanged = (resolved == null && tradingAccountId == null)
                || (resolved != null && resolved.equals(tradingAccountId));
        if (unchanged) {
            if ("init".equals(reason) || "selection".equals(reason)) {
                logAccountState(reason, resolved);
            }
            return;
        }
        tradingAccountId = resolved;
        logAccountState(reason, resolved);
    }

    private void logAccountState(String reason, String accountId) {
        String detail = "null(primary/unspecified)";
        if (accountId != null) {
            AccountInfo info = accountListManager.getAccountById(accountId);
            if (info != null) {
                detail = accountId + " name=" + info.shortName
                        + " summary=" + info.summary
                        + " sim=" + info.isSimulated()
                        + " primary=" + info.isPrimary;
            } else {
                detail = accountId;
            }
        }
        Log.info("MAT account [" + reason + "] " + detail
                + " accountsKnown=" + accountListManager.getAccounts().size());
    }

    /**
     * Prefer Bookmap Account dropdown for this alias (Simulator on Replay).
     * Falls back to null so Bookmap uses the provider primary / only account.
     */
    private String resolveAccountId() {
        if (!Boolean.TRUE.equals(useSelectedAccount)) {
            return null;
        }
        if (primaryAlias == null) {
            return null;
        }
        Optional<String> selected = accountListManager.getSelectedAccountIdForAlias(primaryAlias);
        if (selected.isPresent()) {
            String id = selected.get();
            return (id == null || id.isEmpty()) ? null : id;
        }
        // No selection message yet — if exactly one account exists for alias, use it
        // (typical Replay: only Simulator).
        Set<AccountInfo> forAlias = accountListManager.getAccountsByAlias(primaryAlias);
        if (forAlias != null && forAlias.size() == 1) {
            return forAlias.iterator().next().id;
        }
        Set<AccountInfo> all = accountListManager.getAccounts();
        if (all != null && all.size() == 1) {
            return all.iterator().next().id;
        }
        return null;
    }

    private InstrumentState newState(String role) {
        return newState(role, pips);
    }

    private InstrumentState newState(String role, double instrumentPips) {
        InstrumentState s = new InstrumentState();
        s.role = role;
        s.instrumentPips = instrumentPips > 0 ? instrumentPips : 0.25;
        int bar = barMs == null ? 100 : barMs.intValue();
        int trades = minTrades == null ? 6 : minTrades.intValue();
        double vol = minVol == null ? 25.0 : minVol;
        double imb = minImbalance == null ? 0.55 : minImbalance;
        // Min progress is configured in price points; API trade prices are tick indices.
        double progPts = minProgressPts == null ? s.instrumentPips : minProgressPts;
        double progApi = PriceTicks.pointsToApi(progPts, s.instrumentPips, true);
        int cool = cooldownBars == null ? 3 : cooldownBars.intValue();
        s.burst = new TapeBurstTracker(bar, trades, vol, imb, progApi, cool);
        int range = cdRangeTicks == null ? 22 : cdRangeTicks.intValue();
        int sma = cdSmaLen == null ? 100 : cdSmaLen.intValue();
        // CD range bars: price in ticks → tickSize=1 so range = N ticks
        s.cd = new CumulativeDeltaRange(range, 1.0, sma, 5, 0.35);
        return s;
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : info.symbol;
        double ipips = info.pips > 0 ? info.pips : 0.25;
        InstrumentState state = byAlias.computeIfAbsent(alias, a -> newState("OTHER", ipips));
        String role = InstrumentClassifier.classify(info);
        state.role = role;
        state.instrumentPips = ipips;
        Log.info(String.format(
                "MAT instrument added alias=%s symbol=%s full=%s role=%s pips=%.4f",
                alias,
                info.symbol,
                info.fullName,
                role,
                ipips));
        if (alias.equals(primaryAlias) && info.pips > 0) {
            pips = info.pips;
            state.instrumentPips = pips;
            // Rebuild detectors with correct primary pips / tick-scaled thresholds
            InstrumentState rebuilt = newState(role, pips);
            rebuilt.lastPrice = state.lastPrice;
            byAlias.put(alias, rebuilt);
        }
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        byAlias.computeIfAbsent(alias, a -> newState("OTHER"));
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        // Replay rewind / catchup skip: reset detectors so we don't keep future bursts
        if (lastSeenTsNs > 0 && nanoseconds + 500_000_000L < lastSeenTsNs) {
            Log.info(String.format(
                    "MAT rewind/reset detected prevTs=%d newTs=%d — clearing burst/CD state",
                    lastSeenTsNs, nanoseconds));
            resetAllDetectors();
        }
        this.nowNs = nanoseconds;
        this.lastSeenTsNs = nanoseconds;
    }

    private void resetAllDetectors() {
        for (InstrumentState s : byAlias.values()) {
            if (s.burst != null) {
                s.burst.reset();
            }
            if (s.cd != null) {
                s.cd.reset();
            }
        }
        lastSignalNs = 0;
        nqBurstCount = esBurstCount = 0;
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        InstrumentState state = byAlias.get(currentAlias);
        if (state == null) {
            state = newState("OTHER");
            byAlias.put(currentAlias, state);
        }
        state.lastPrice = price;
        long ts = nowNs > 0 ? nowNs : System.nanoTime();
        boolean buyAgg = tradeInfo.isBidAggressor;
        Burst burst = state.burst.onTrade(ts, price, size, buyAgg);
        state.cd.onTrade(price, size, buyAgg);

        if ("ES".equals(state.role)) {
            esTradeCount++;
            if (burst != null) {
                esBurstCount++;
                if (esBurstCount <= 5 || esBurstCount % 50 == 0) {
                    double chartPx = PriceTicks.toChartPoints(burst.closePrice, state.instrumentPips);
                    Log.info(String.format(
                            "MAT ES burst#%d side=%d n=%d vol=%.0f imb=%.2f px=%.2f (api=%.0f) %s",
                            esBurstCount, burst.side, burst.nTrades, burst.totalVol,
                            burst.imbalance, chartPx, burst.closePrice, formatReplayTime(ts)));
                }
            }
            return;
        }

        if ("NQ".equals(state.role) || currentAlias.equals(primaryAlias)) {
            nqTradeCount++;
        }

        if (isPrimaryNq(state)) {
            maybeExtendTp(state);
            tryPendingReverse();
        }

        // Only evaluate entries when the primary (NQ) chart closes a burst bar
        if (burst == null || !isPrimaryNq(state)) {
            return;
        }
        nqBurstCount++;
        maybeSignal(burst, state);
    }

    private boolean isPrimaryNq(InstrumentState state) {
        if ("NQ".equals(state.role)) {
            return currentAlias != null && currentAlias.equals(primaryAlias);
        }
        // Attached chart treated as NQ if unclassified
        return currentAlias != null && currentAlias.equals(primaryAlias)
                && ("NQ".equals(state.role) || "OTHER".equals(state.role));
    }

    private void maybeSignal(Burst nqBurst, InstrumentState nqState) {
        syncSideFilterFromParams("params");
        EntryEval eval = evaluateEntry(nqBurst.side, nqBurst, nqState, false);

        esLagIndicator.addPoint(eval.lagMs >= Long.MAX_VALUE / 2 ? -1.0 : eval.lagMs);
        cdTrendIndicator.addPoint(nqState.cd.getMaTrend());

        if (!eval.ok) {
            signalIndicator.addPoint(0);
            if (nqBurst.tsNs - rejectLogNs >= REJECT_LOG_INTERVAL_NS) {
                rejectLogNs = nqBurst.tsNs;
                double maxLag = maxEsLagMs == null ? 0.0 : maxEsLagMs;
                Log.info(String.format(
                        "MAT REJECT %s nqBurst#%d side=%d px=%.2f time=%s lagMs=%s maxLag=%.0f syncMs=%.0f "
                                + "esTrades=%d esBursts=%d nqTrades=%d nqBursts=%d aliases=%d",
                        eval.reason,
                        nqBurstCount,
                        nqBurst.side,
                        PriceTicks.toChartPoints(nqBurst.closePrice, pips),
                        formatReplayTime(nqBurst.tsNs),
                        eval.lagMs >= Long.MAX_VALUE / 2 ? "none" : Long.toString(eval.lagMs),
                        maxLag,
                        syncMs == null ? 1500.0 : syncMs,
                        esTradeCount,
                        esBurstCount,
                        nqTradeCount,
                        nqBurstCount,
                        byAlias.size()));
            }
            return;
        }

        fireSignal(nqBurst.side, nqBurst.closePrice, eval.lagMs, nqState);
    }

    private static final class EntryEval {
        final boolean ok;
        final String reason;
        final long lagMs;

        EntryEval(boolean ok, String reason, long lagMs) {
            this.ok = ok;
            this.reason = reason;
            this.lagMs = lagMs;
        }
    }

    /**
     * Shared entry gate for signals, reverse-if-OK, and TP-extend.
     * @param ignoreFlat when true, skip flat-only / flatLocked (in-trade checks)
     */
    private EntryEval evaluateEntry(int side, Burst nqBurst, InstrumentState nqState, boolean ignoreFlat) {
        if (nqBurst == null || nqState == null) {
            return new EntryEval(false, "no_burst", Long.MAX_VALUE);
        }
        if (nqBurst.side != side) {
            return new EntryEval(false, "burst_wrong_side", Long.MAX_VALUE);
        }

        InstrumentState es = findByRole("ES");
        long syncNs = (long) ((syncMs == null ? 1500.0 : syncMs) * 1_000_000.0);
        long lagMs = es == null ? Long.MAX_VALUE : es.burst.lagMsTo(nqBurst.side, nqBurst.tsNs);
        boolean esPresent = es != null;
        boolean esRecent = esPresent && es.burst.hasRecentBurst(nqBurst.side, nqBurst.tsNs, syncNs);
        double maxLag = maxEsLagMs == null ? 0.0 : maxEsLagMs;
        boolean lagOk = maxLag <= 0 || lagMs <= maxLag;

        if (!esPresent) {
            return new EntryEval(false, "no_ES_instrument", lagMs);
        }
        if (!esRecent) {
            return new EntryEval(false, "ES_no_same_side_burst_in_sync", lagMs);
        }
        if (!lagOk) {
            return new EntryEval(false, "ES_lag_gt_max", lagMs);
        }

        if (Boolean.TRUE.equals(requireCdPullback)) {
            boolean cdOk = side > 0 ? nqState.cd.isPullbackLong() : nqState.cd.isPullbackShort();
            if (!cdOk) {
                return new EntryEval(false, "cd_pullback_fail", lagMs);
            }
        }

        long coolNs = (long) ((signalCooldownSec == null ? 5.0 : signalCooldownSec) * 1_000_000_000.0);
        long refTs = nqBurst.tsNs > 0 ? nqBurst.tsNs : (nowNs > 0 ? nowNs : System.nanoTime());
        if (refTs - lastSignalNs < coolNs) {
            return new EntryEval(false, "signal_cooldown", lagMs);
        }

        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(side)) {
            return new EntryEval(false, side > 0 ? "longs_disabled" : "shorts_disabled", lagMs);
        }

        if (!ignoreFlat) {
            int flatPos = flatCheckPosition();
            boolean flat = !Boolean.TRUE.equals(flatOnly) || (flatPos == 0 && !flatLocked);
            if (!flat) {
                return new EntryEval(false, "not_flat(pos=" + flatPos + ",locked=" + flatLocked + ")", lagMs);
            }
        }
        return new EntryEval(true, "ok", lagMs);
    }

    /** Entry check using the latest NQ burst of the requested side (for reverse / extend). */
    private EntryEval evaluateEntryFromLastBurst(int side, boolean ignoreFlat) {
        InstrumentState nq = byAlias.get(primaryAlias);
        if (nq == null || nq.burst == null) {
            return new EntryEval(false, "no_nq", Long.MAX_VALUE);
        }
        Burst last = nq.burst.getLastBurst();
        if (last == null || last.side != side) {
            return new EntryEval(false, "no_same_side_burst", Long.MAX_VALUE);
        }
        long ageNs = (nowNs > 0 ? nowNs : System.nanoTime()) - last.tsNs;
        long syncNs = (long) ((syncMs == null ? 1500.0 : syncMs) * 1_000_000.0);
        if (ageNs > syncNs) {
            return new EntryEval(false, "burst_stale", Long.MAX_VALUE);
        }
        return evaluateEntry(side, last, nq, ignoreFlat);
    }

    private void clearTradeContext(String via) {
        openSide = 0;
        entryFillPx = 0;
        tpOrderId = null;
        slOrderId = null;
        currentTpPx = 0;
        tpExtendCount = 0;
        tpExtendArmed = true;
        if (!"pending_reverse".equals(via)) {
            pendingReverse = false;
            pendingReverseSide = 0;
        }
        Log.info("MAT trade context cleared (" + via + ")");
    }

    /** Position used for flat-only: StatusInfo if set, else net fills. */
    private int flatCheckPosition() {
        return position != 0 ? position : netFills;
    }

    private void fireSignal(int side, double apiPrice, long esLagMs, InstrumentState nqState) {
        lastSignalNs = nowNs > 0 ? nowNs : System.nanoTime();
        lastSignalSide = side;
        signalThisInterval = true;
        signalIndicator.addPoint(side);

        double chartPrice = PriceTicks.toChartPoints(apiPrice, pips);
        if (side > 0) {
            longMarker.addPoint(chartPrice);
            shortMarker.addPoint(Double.NaN);
        } else {
            shortMarker.addPoint(chartPrice);
            longMarker.addPoint(Double.NaN);
        }

        boolean wantTrade = Boolean.TRUE.equals(enableTrading);
        boolean willTrade = wantTrade && realtimeStarted && api != null;
        Log.info(String.format(
                "MAT SIGNAL %s @%.2f time=%s apiPx=%.0f esLagMs=%d cdTrend=%d cdPB=%s "
                        + "trading=%s realtime=%s pos=%d sideFilter=%s account=%s",
                side > 0 ? "LONG" : "SHORT",
                chartPrice,
                formatReplayTime(lastSignalNs),
                apiPrice,
                esLagMs,
                nqState.cd.getMaTrend(),
                side > 0 ? nqState.cd.isPullbackLong() : nqState.cd.isPullbackShort(),
                wantTrade,
                realtimeStarted,
                flatCheckPosition(),
                sideFilterLabel(),
                tradingAccountId == null ? "null" : tradingAccountId));

        if (willTrade) {
            sendBracketOrder(side > 0, chartPrice, false);
        } else if (wantTrade && !realtimeStarted) {
            Log.info("MAT ORDER skipped (catchup/history — waiting for realtime)");
        }
    }

    private String sideFilterLabel() {
        return MatSideFilter.label();
    }

    /**
     * Push panel Allow longs/shorts into {@link MatSideFilter} when they change.
     * Hotkeys update the filter directly; unchanged panel values do not overwrite them.
     */
    private void syncSideFilterFromParams(String via) {
        Boolean L = allowLongs;
        Boolean S = allowShorts;
        if (Objects.equals(lastSyncedAllowLongs, L) && Objects.equals(lastSyncedAllowShorts, S)) {
            return;
        }
        lastSyncedAllowLongs = L;
        lastSyncedAllowShorts = S;
        MatSideFilter.set(Boolean.TRUE.equals(L), Boolean.TRUE.equals(S), via);
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        // Allow a fresh entry right after catchup; detectors stay warm.
        lastSignalNs = 0;
        refreshTradingAccountId("realtime");
        Log.info(String.format(
                "MAT realtime start — orders %s side=%s account=%s (pos=%d). "
                        + "Play/forward from here for live signals.",
                Boolean.TRUE.equals(enableTrading) ? "ENABLED" : "disabled (signal-only)",
                sideFilterLabel(),
                tradingAccountId == null ? "null(primary)" : tradingAccountId,
                position));
    }

    private static String formatReplayTime(long tsNs) {
        if (tsNs <= 0) {
            return "unknown";
        }
        try {
            long sec = tsNs / 1_000_000_000L;
            long nano = tsNs % 1_000_000_000L;
            ZonedDateTime zdt = Instant.ofEpochSecond(sec, nano).atZone(UK);
            return TIME_FMT.format(zdt);
        } catch (Exception e) {
            return "ns=" + tsNs;
        }
    }

    private void flattenPosition() {
        int pos = flatCheckPosition();
        int size = Math.abs(pos);
        if (size <= 0) {
            size = Math.abs(netFills);
        }
        if (size <= 0) {
            Log.info("MAT FLATTEN: already flat (pos=" + position + " netFills=" + netFills + ")");
            return;
        }
        if (api == null || primaryAlias == null) {
            Log.info("MAT FLATTEN: no api/alias");
            return;
        }
        if (!Boolean.TRUE.equals(enableTrading)) {
            Log.info("MAT FLATTEN: trading disabled — side filter already NONE; enable trading to market-close");
            return;
        }
        boolean isBuy = pos < 0;
        refreshTradingAccountId("flatten");
        flatLocked = true;
        pendingReverse = false;
        try {
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias,
                    isBuy,
                    size,
                    OrderDuration.GTC,
                    null,
                    Double.NaN,
                    Double.NaN,
                    0,
                    0,
                    0,
                    0,
                    false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            api.sendOrder(builder.build());
            Log.info(String.format(
                    "MAT FLATTEN sent %s size=%d (close pos=%d netFills=%d) account=%s",
                    isBuy ? "BUY" : "SELL",
                    size,
                    pos,
                    netFills,
                    tradingAccountId == null ? "null(primary)" : tradingAccountId));
        } catch (Throwable t) {
            Log.info("MAT FLATTEN failed: " + t);
        }
    }

    private void sendBracketOrder(boolean isBuy, double entryPricePoints, boolean force) {
        int size = Math.max(1, orderSize == null ? 1 : orderSize.intValue());
        double tpPts = takeProfitPts == null ? 10.0 : takeProfitPts;
        double slPts = stopLossPts == null ? 10.0 : stopLossPts;
        double pip = pips > 0 ? pips : 0.25;

        // Bookmap brackets use tick offsets from fill (not absolute chart prices).
        // AbsoluteBracketsValuesOrderSendParameters was arriving with offset=0 and
        // never spawned TP/SL children in Replay SIM.
        boolean brackets = Boolean.TRUE.equals(useBrackets);
        int tpTicks = brackets ? Math.max(1, (int) Math.round(tpPts / pip)) : 0;
        int slTicks = brackets ? Math.max(1, (int) Math.round(slPts / pip)) : 0;
        double tpPx = isBuy ? entryPricePoints + tpPts : entryPricePoints - tpPts;
        double slPx = isBuy ? entryPricePoints - slPts : entryPricePoints + slPts;

        // Lock flat-only BEFORE send so same-thread / fast replay cannot pyramid
        // while PositionListener still reports 0. force=true for stop-reverse.
        if (!force && Boolean.TRUE.equals(flatOnly) && (flatCheckPosition() != 0 || flatLocked)) {
            Log.info("MAT ORDER blocked flat-only pos=" + position
                    + " netFills=" + netFills + " locked=" + flatLocked);
            return;
        }
        flatLocked = true;
        refreshTradingAccountId("order");

        try {
            // clientId null → Bookmap generates one. Account from dropdown when known.
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias,
                    isBuy,
                    size,
                    OrderDuration.GTC,
                    null,
                    Double.NaN,
                    Double.NaN,
                    tpTicks,
                    slTicks,
                    0,
                    0,
                    false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            SimpleOrderSendParameters order = builder.build();
            api.sendOrder(order);
            // Expected TP for extend tracking until working LMT arrives
            openSide = isBuy ? 1 : -1;
            entryFillPx = entryPricePoints;
            currentTpPx = tpPx;
            tpOrderId = null;
            slOrderId = null;
            tpExtendCount = 0;
            tpExtendArmed = true;
            Log.info(String.format(
                    "MAT ORDER sent %s size=%d entry~%.2f brackets=%s tp=%d ticks (~%.2f pts @%.2f) "
                            + "sl=%d ticks (~%.2f pts @%.2f) account=%s locked=%s netFills=%d force=%s time=%s",
                    isBuy ? "BUY" : "SELL",
                    size,
                    entryPricePoints,
                    brackets,
                    tpTicks,
                    tpPts,
                    tpPx,
                    slTicks,
                    slPts,
                    slPx,
                    tradingAccountId == null ? "null(primary)" : tradingAccountId,
                    flatLocked,
                    netFills,
                    force,
                    formatReplayTime(nowNs)));
        } catch (Throwable t) {
            flatLocked = false;
            Log.info("MAT ORDER failed: " + t);
        }
    }

    private InstrumentState findByRole(String role) {
        for (InstrumentState s : byAlias.values()) {
            if (role.equals(s.role)) {
                return s;
            }
        }
        return null;
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        // Bottom signal is a spike series — return to 0 between fires.
        // Do NOT NaN-out PRIMARY markers here: that erased dots within ~100ms
        // so the chart looked like "no signals" even when MAT SIGNAL logged.
        if (!signalThisInterval) {
            signalIndicator.addPoint(0);
        }
        signalThisInterval = false;

        InstrumentState nq = byAlias.get(primaryAlias);
        InstrumentState es = findByRole("ES");
        if (nq != null) {
            cdTrendIndicator.addPoint(nq.cd.getMaTrend());
        }
        if (es != null && nq != null && nq.burst.getLastBurst() != null) {
            long lag = es.burst.lagMsTo(nq.burst.getLastBurst().side, nowNs);
            esLagIndicator.addPoint(lag >= Long.MAX_VALUE / 2 ? -1.0 : lag);
        }
    }

    @Override
    public void onPositionUpdate(StatusInfo statusInfo) {
        if (statusInfo == null) {
            return;
        }
        // Ignore other accounts when we are targeting a specific one.
        if (tradingAccountId != null
                && statusInfo.accountId != null
                && !tradingAccountId.equals(statusInfo.accountId)) {
            return;
        }
        position = statusInfo.position;
        if (statusInfo.position == 0
                && statusInfo.workingBuys == 0
                && statusInfo.workingSells == 0) {
            flatLocked = false;
            netFills = 0;
            if (openSide != 0 && !pendingReverse) {
                clearTradeContext("flat");
            }
            tryPendingReverse();
        } else if (statusInfo.position != 0
                || statusInfo.workingBuys > 0
                || statusInfo.workingSells > 0) {
            flatLocked = true;
            if (statusInfo.position != 0) {
                netFills = statusInfo.position;
            }
        }
        Log.info(String.format(
                "MAT POS update pos=%d avg=%.2f wb=%d ws=%d unreal=%.2f locked=%s openSide=%d account=%s",
                statusInfo.position,
                statusInfo.averagePrice,
                statusInfo.workingBuys,
                statusInfo.workingSells,
                statusInfo.unrealizedPnl,
                flatLocked,
                openSide,
                statusInfo.accountId));
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
        if (tradingAccountId != null
                && order.accountId != null
                && !tradingAccountId.equals(order.accountId)) {
            return;
        }

        trackBracketLeg(order);

        int prevFilled = filledByOrderId.getOrDefault(order.orderId, 0);
        int filled = order.filled;
        if (filled <= prevFilled) {
            filledByOrderId.put(order.orderId, Math.max(prevFilled, filled));
            return;
        }
        int delta = filled - prevFilled;
        filledByOrderId.put(order.orderId, filled);
        netFills += order.isBuy ? delta : -delta;
        if (netFills == 0 && position == 0) {
            flatLocked = false;
        } else {
            flatLocked = true;
        }
        Log.info(String.format(
                "MAT FILL %s order=%s type=%s delta=%d filled=%d netFills=%d statusPos=%d locked=%s account=%s",
                order.isBuy ? "BUY" : "SELL",
                order.orderId,
                order.type,
                delta,
                filled,
                netFills,
                position,
                flatLocked,
                order.accountId));

        boolean isStopFill = order.type == OrderType.STP || order.type == OrderType.STP_LMT
                || (slOrderId != null && slOrderId.equals(order.orderId));
        boolean isTpFill = order.type == OrderType.LMT
                && tpOrderId != null && tpOrderId.equals(order.orderId);

        if (isStopFill && openSide != 0) {
            onStopFilled(order);
        } else if (isTpFill || (netFills == 0 && position == 0 && openSide != 0 && !pendingReverse)) {
            clearTradeContext(isTpFill ? "tp_fill" : "flat_fill");
        }
        tryPendingReverse();
    }

    private void trackBracketLeg(OrderInfoUpdate order) {
        if (order.status == OrderStatus.CANCELLED || order.status == OrderStatus.REJECTED) {
            return;
        }
        OrderType type = order.type;
        if (type == null) {
            return;
        }

        // Entry: market (or leading) fill sets open side if still flat context
        if (openSide == 0 && order.filled > 0 && (type == OrderType.MKT || order.fakeMarketOrder)) {
            openSide = order.isBuy ? 1 : -1;
            entryFillPx = PriceTicks.toChartPoints(order.averageFillPrice, pips);
            double tpPts = takeProfitPts == null ? 10.0 : takeProfitPts;
            currentTpPx = openSide > 0 ? entryFillPx + tpPts : entryFillPx - tpPts;
            tpExtendCount = 0;
            tpExtendArmed = true;
            Log.info(String.format(
                    "MAT trade open %s entry~%.2f expectedTp~%.2f",
                    openSide > 0 ? "LONG" : "SHORT", entryFillPx, currentTpPx));
            return;
        }

        if (openSide == 0) {
            return;
        }

        // Exit legs are opposite the position
        boolean exitIsBuy = openSide < 0;
        if (order.isBuy != exitIsBuy) {
            return;
        }

        if (type == OrderType.LMT && (order.status == OrderStatus.WORKING
                || order.status == OrderStatus.PENDING_SUBMIT
                || order.status == OrderStatus.PENDING_MODIFY
                || order.unfilled > 0)) {
            tpOrderId = order.orderId;
            if (!Double.isNaN(order.limitPrice) && order.limitPrice != 0) {
                currentTpPx = PriceTicks.toChartPoints(order.limitPrice, pips);
            }
            Log.info("MAT TP leg id=" + tpOrderId + " px~" + currentTpPx);
        } else if ((type == OrderType.STP || type == OrderType.STP_LMT)
                && (order.status == OrderStatus.WORKING
                || order.status == OrderStatus.PENDING_SUBMIT
                || order.status == OrderStatus.PENDING_MODIFY
                || order.unfilled > 0)) {
            slOrderId = order.orderId;
            Log.info("MAT SL leg id=" + slOrderId
                    + " stop~" + PriceTicks.toChartPoints(order.stopPrice, pips));
        }
    }

    private void onStopFilled(OrderInfoUpdate order) {
        int closedSide = openSide;
        Log.info(String.format(
                "MAT STOP filled order=%s closedSide=%s reverseOn=%s onlyIfOk=%s",
                order.orderId,
                closedSide > 0 ? "LONG" : (closedSide < 0 ? "SHORT" : "FLAT"),
                Boolean.TRUE.equals(reverseOnStop),
                Boolean.TRUE.equals(reverseOnlyIfEntryOk)));

        if (!Boolean.TRUE.equals(reverseOnStop) || closedSide == 0) {
            clearTradeContext("stop_fill");
            return;
        }

        int reverseSide = -closedSide;
        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(reverseSide)) {
            Log.info("MAT REVERSE skipped side_filter " + MatSideFilter.label());
            clearTradeContext("stop_no_reverse");
            return;
        }

        if (Boolean.TRUE.equals(reverseOnlyIfEntryOk)) {
            EntryEval eval = evaluateEntryFromLastBurst(reverseSide, true);
            if (!eval.ok) {
                Log.info("MAT REVERSE skipped entry_not_ok reason=" + eval.reason);
                clearTradeContext("stop_no_reverse");
                return;
            }
        }

        pendingReverse = true;
        pendingReverseSide = reverseSide;
        clearTradeContext("pending_reverse");
        Log.info("MAT REVERSE armed side=" + (reverseSide > 0 ? "LONG" : "SHORT"));
    }

    private void tryPendingReverse() {
        if (!pendingReverse) {
            return;
        }
        if (!Boolean.TRUE.equals(enableTrading) || !realtimeStarted || api == null) {
            Log.info("MAT REVERSE deferred trading=" + enableTrading + " realtime=" + realtimeStarted);
            return;
        }
        if (flatCheckPosition() != 0) {
            return;
        }
        int side = pendingReverseSide;
        pendingReverse = false;
        pendingReverseSide = 0;

        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(side)) {
            Log.info("MAT REVERSE aborted side_filter " + MatSideFilter.label());
            return;
        }

        InstrumentState nq = byAlias.get(primaryAlias);
        double px = nq != null
                ? PriceTicks.toChartPoints(nq.lastPrice, pips)
                : entryFillPx;
        if (px <= 0 && nq != null) {
            px = PriceTicks.toChartPoints(nq.lastPrice, nq.instrumentPips);
        }
        Log.info("MAT REVERSE send " + (side > 0 ? "LONG" : "SHORT") + " @~" + px);
        lastSignalNs = nowNs > 0 ? nowNs : System.nanoTime();
        lastSignalSide = side;
        sendBracketOrder(side > 0, px, true);
    }

    private void maybeExtendTp(InstrumentState nqState) {
        if (!Boolean.TRUE.equals(extendTpIfFavourable)
                || !Boolean.TRUE.equals(useBrackets)
                || openSide == 0
                || tpOrderId == null
                || api == null
                || !realtimeStarted) {
            return;
        }

        double proximity = tpExtendProximityPts == null ? 2.0 : tpExtendProximityPts;
        if (proximity < 0) {
            proximity = 0;
        }
        int maxExt = maxTpExtends == null ? 3 : maxTpExtends.intValue();
        if (maxExt > 0 && tpExtendCount >= maxExt) {
            return;
        }

        double lastPx = PriceTicks.toChartPoints(nqState.lastPrice, pips);
        double dist = Math.abs(lastPx - currentTpPx);

        // Clear debounce when price backs away
        if (dist > proximity * 1.5) {
            tpExtendArmed = true;
            return;
        }
        if (!tpExtendArmed || dist > proximity) {
            return;
        }

        // Must still be approaching TP, not already through it
        boolean throughTp = openSide > 0 ? lastPx >= currentTpPx : lastPx <= currentTpPx;
        if (throughTp) {
            return;
        }

        EntryEval eval = evaluateEntryFromLastBurst(openSide, true);
        if (!eval.ok) {
            return;
        }

        double tpPts = takeProfitPts == null ? 10.0 : takeProfitPts;
        double newTp = openSide > 0 ? currentTpPx + tpPts : currentTpPx - tpPts;
        try {
            OrderMoveParameters move = new OrderMoveParameters.Builder(tpOrderId)
                    .setOrderEntryType(OrderEntryType.AUTO)
                    .setStopPrice(Double.NaN)
                    .setLimitPrice(newTp)
                    .build();
            api.updateOrder(move);
            tpExtendCount++;
            currentTpPx = newTp;
            tpExtendArmed = false;
            Log.info(String.format(
                    "MAT TP EXTEND #%d %s oldDist=%.2f newTp=%.2f last=%.2f reason=favourable lagMs=%s",
                    tpExtendCount,
                    openSide > 0 ? "LONG" : "SHORT",
                    dist,
                    newTp,
                    lastPx,
                    eval.lagMs >= Long.MAX_VALUE / 2 ? "n/a" : Long.toString(eval.lagMs)));
        } catch (Throwable t) {
            Log.info("MAT TP EXTEND failed: " + t);
        }
    }

    @Override
    public void onOrderExecuted(velox.api.layer1.data.ExecutionInfo executionInfo) {
        // Position synced via onOrderUpdated / onPositionUpdate.
    }

    @Override
    public void stop() {
        MatTradeControl.unregister(flattenAction);
        if (sideHotkeysInstalled) {
            MatSideHotkeys.uninstall();
            sideHotkeysInstalled = false;
        }
        if (provider != null && adminListener != null) {
            try {
                provider.removeListener(adminListener);
            } catch (Throwable t) {
                Log.info("MAT account listener remove failed: " + t);
            }
        }
        byAlias.clear();
        Log.info("Momentum AutoTrade stopped. lastSignalSide=" + lastSignalSide
                + " sideFilter=" + sideFilterLabel());
    }
}
