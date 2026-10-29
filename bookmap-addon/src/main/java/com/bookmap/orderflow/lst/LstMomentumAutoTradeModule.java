package com.bookmap.orderflow.lst;

import java.awt.Color;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.SliceEventTracker;
import com.bookmap.orderflow.core.SliceEventTracker.Config;
import com.bookmap.orderflow.core.SliceEventTracker.EpisodeSnapshot;
import com.bookmap.orderflow.core.SliceEventTracker.Signal;
import com.bookmap.orderflow.core.SliceEventTracker.SignalKind;
import com.bookmap.orderflow.core.SliceEventTracker.State;

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
import velox.api.layer1.data.OrderInfoUpdate;
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
import velox.api.layer1.simplified.DepthDataListener;
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
 * LST Momentum AutoTrade — RULE_SPEC_V1 frozen brackets + structured CSV logs.
 *
 * <p>Attach on <b>NQ</b> with <b>ES</b> open. Default: trading <b>ON</b> for SIM
 * replay validation (turn off for signal-only). Orders only after
 * {@link #onRealtimeStart()} (replay cursor / live), not during catchup load.
 *
 * <p>Bracket (train freeze): level-based SL (signal stop, clamped 4–40), TP 16,
 * time-stop 60 s. Logs:
 * {@code results/MOMENTUM_SLICE/logs/lst_at_signals.csv} and
 * {@code lst_at_trades.csv}.
 *
 * <p>Detach indicator-only {@code LST Momentum} / {@code LST Pitch-and-Catch}
 * while testing this module — duplicates double the Loading-events cost.
 */
@Layer1SimpleAttachable
@Layer1TradingStrategy
@Layer1MultiAccountTradingSupported
@Layer1StrategyName("LST Momentum AutoTrade")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class LstMomentumAutoTradeModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        HistoricalModeListener,
        PositionListener,
        OrdersListener,
        SliceEventTracker.Listener {

    @Parameter(name = "Use frozen V1 preset", step = 1.0)
    Boolean frozenPreset = true;

    @Parameter(name = "Enable trading")
    Boolean enableTrading = true;

    /**
     * Bookmap feeds the whole session during Loading events before
     * {@code onRealtimeStart}. For SIM replay validation, leave this ON so
     * the 15:24 catchup fire can send orders. Turn OFF for live (orders only
     * after realtime).
     */
    @Parameter(name = "Orders during catchup (SIM replay)")
    Boolean ordersDuringCatchup = true;

    @Parameter(name = "Order size", step = 1.0)
    Double orderSize = 1.0;

    @Parameter(name = "Flat only")
    Boolean flatOnly = true;

    @Parameter(name = "TP pts", step = 0.25)
    Double tpPts = 16.0;

    @Parameter(name = "SL min pts", step = 0.25)
    Double slMinPts = 4.0;

    @Parameter(name = "SL max pts", step = 0.25)
    Double slMaxPts = 40.0;

    @Parameter(name = "Time stop sec (0=off)", step = 1.0)
    Double timeStopSec = 60.0;

    @Parameter(name = "Signal cooldown sec", step = 1.0)
    Double signalCooldownSec = 30.0;

    @Parameter(name = "Allow longs")
    Boolean allowLongs = true;

    @Parameter(name = "Allow shorts")
    Boolean allowShorts = true;

    @Parameter(name = "Use selected account")
    Boolean useSelectedAccount = true;

    @Parameter(name = "Signal log path")
    String signalLogPath = "C:/Work/Mat/bookmap/results/MOMENTUM_SLICE/logs/lst_at_signals.csv";

    @Parameter(name = "Trade log path")
    String tradeLogPath = "C:/Work/Mat/bookmap/results/MOMENTUM_SLICE/logs/lst_at_trades.csv";

    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final DateTimeFormatter TIME_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS z");

    private Api api;
    private Layer1ApiProvider provider;
    private Layer1ApiAdminAdapter adminListener;
    private final AccountListManager accountListManager = new AccountListManager();
    private String primaryAlias;
    private String currentAlias;
    private final Map<String, String> roleByAlias = new HashMap<>();
    private double pips = 0.25;
    private long nowNs;
    private boolean realtimeStarted;
    private SliceEventTracker tracker;

    private Indicator stateIndicator;
    private Indicator signalIndicator;
    private Indicator fireLongMarker;
    private Indicator fireShortMarker;
    private Indicator levelMarker;
    private Indicator stopMarker;

    private LstCsvLog signalLog;
    private LstCsvLog tradeLog;

    private long lastSignalNs;
    private long fireUntilNs;
    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private double armedLevel = Double.NaN;
    private double armedStop = Double.NaN;

    private int position;
    private int netFills;
    private boolean flatLocked;
    private final Map<String, Integer> filledByOrderId = new HashMap<>();
    private volatile String tradingAccountId;
    private int openSide;
    private double entryFillPx;
    private long entryTsNs;
    private double openTpPts;
    private double openSlPts;
    private String openSignalId;
    private long pendingTimeStopNs;
    private State lastDiagState = State.IDLE;
    private long lastDiagNs;
    private boolean warnedSecondaryNq;
    private boolean warnedNoEs;
    private final java.util.Set<String> aliasesSeen = new java.util.HashSet<>();

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.api = api;
        this.primaryAlias = alias;
        this.currentAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        this.realtimeStarted = false;
        roleByAlias.put(alias, InstrumentClassifier.classify(info));

        Config cfg = new Config();
        cfg.enableMomentum = true;
        cfg.enablePitchCatch = false;
        if (Boolean.TRUE.equals(frozenPreset)) {
            cfg.trendMinPts = 0.0;
            cfg.domVsMedMin = 4.0;
            cfg.stopBufferPts = 2.0;
            cfg.cooldownNs = 30_000_000_000L;
        }
        cfg.enableLongs = !Boolean.FALSE.equals(allowLongs);
        cfg.enableShorts = !Boolean.FALSE.equals(allowShorts);
        tracker = new SliceEventTracker(cfg, this);
        tracker.setTickSize(pips);
        tracker.setHistoryMode(true);

        stateIndicator = api.registerIndicator("LST-AT State", GraphType.BOTTOM);
        signalIndicator = api.registerIndicator("LST-AT Signal", GraphType.BOTTOM);
        fireLongMarker = api.registerIndicator("LST-AT Fire Long", GraphType.PRIMARY);
        fireShortMarker = api.registerIndicator("LST-AT Fire Short", GraphType.PRIMARY);
        levelMarker = api.registerIndicator("LST-AT Level", GraphType.PRIMARY);
        stopMarker = api.registerIndicator("LST-AT Stop", GraphType.PRIMARY);
        stateIndicator.setColor(new Color(200, 200, 200));
        signalIndicator.setColor(new Color(255, 220, 80));
        fireLongMarker.setColor(new Color(0, 255, 120));
        fireShortMarker.setColor(new Color(255, 80, 80));
        levelMarker.setColor(new Color(0, 180, 255));
        stopMarker.setColor(new Color(255, 140, 0));
        AxisGroup bottom = new AxisGroup();
        bottom.add(stateIndicator);
        bottom.add(signalIndicator);

        signalLog = new LstCsvLog("LST-AT signals", signalLogPath);
        signalLog.writeHeaderOnce(
                "event,bst,signal_ts_ns,side,entry_ref_px,dominant_level_px,stop_px,"
                        + "trend_5m_pts,dom_vs_med,burst_tps,pullback_ratio,tp_pts,sl_pts,"
                        + "action,reason,realtime");
        tradeLog = new LstCsvLog("LST-AT trades", tradeLogPath);
        tradeLog.writeHeaderOnce(
                "event,bst,ts_ns,side,entry_px,exit_px,pnl_pts,tp_pts,sl_pts,reason,signal_ts_ns");
        signalLog.writeRow(String.format(Locale.US,
                "INIT,,,,,%s,,,,,,,,,,,%s,%s",
                roleByAlias.get(alias),
                Boolean.TRUE.equals(enableTrading) ? "trading_on" : "trading_off",
                Boolean.TRUE.equals(ordersDuringCatchup) ? "catchup_orders_on" : "catchup_orders_off"));

        installAccountListener(api);
        refreshTradingAccountId("init");
        Log.info(String.format(Locale.US,
                "LST-AT init alias=%s role=%s pips=%.4f trading=%s catchupOrders=%s frozen=%s "
                        + "(detach duplicate LST indicator modules to speed Loading events)",
                alias, roleByAlias.get(alias), pips, enableTrading, ordersDuringCatchup, frozenPreset));
    }

    private void installAccountListener(Api api) {
        try {
            provider = api.getProvider();
            if (provider == null) {
                return;
            }
            adminListener = new Layer1ApiAdminAdapter() {
                @Override
                public void onUserMessage(Object data) {
                    boolean updated = accountListManager.onUserMessage(data);
                    if (data instanceof SelectedAccountChangedMessage) {
                        SelectedAccountChangedMessage msg = (SelectedAccountChangedMessage) data;
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
            Log.warn("LST-AT account listener failed: " + t.getMessage());
        }
    }

    private void refreshTradingAccountId(String reason) {
        String resolved = resolveAccountId();
        tradingAccountId = resolved;
        if ("init".equals(reason) || "selection".equals(reason)) {
            Log.info("LST-AT account [" + reason + "] "
                    + (resolved == null ? "null(primary)" : resolved));
        }
    }

    private String resolveAccountId() {
        if (!Boolean.TRUE.equals(useSelectedAccount) || primaryAlias == null) {
            return null;
        }
        try {
            Set<AccountInfo> forAlias = accountListManager.getAccountsByAlias(primaryAlias);
            if (forAlias != null && forAlias.size() == 1) {
                return forAlias.iterator().next().id;
            }
            Set<AccountInfo> all = accountListManager.getAccounts();
            if (all != null && all.size() == 1) {
                return all.iterator().next().id;
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        if (tracker != null) {
            tracker.setHistoryMode(false);
        }
        Log.info("LST-AT realtime start — trading eligible, historyMode=false");
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : info.symbol;
        roleByAlias.put(alias, InstrumentClassifier.classify(info));
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        roleByAlias.putIfAbsent(alias, "OTHER");
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        nowNs = nanoseconds;
        if (tracker != null) {
            tracker.onTime(nanoseconds);
        }
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        EpisodeSnapshot snap = tracker == null ? EpisodeSnapshot.idle() : tracker.snapshot();
        // Diagnostics into signals CSV so agent can debug without Bookmap log.
        if (tracker != null && signalLog != null) {
            boolean stateChanged = snap.state != lastDiagState;
            boolean heartbeat = nowNs > 0 && nowNs - lastDiagNs >= 30_000_000_000L;
            if (stateChanged || heartbeat) {
                lastDiagNs = nowNs;
                String reason = String.format(Locale.US,
                        "state=%s side=%d level=%s dom=%s trend=%s nqTrades=%d esTrades=%d "
                                + "bookLv=%d snaps=%d nqBurst=%d@%d esBurst=%d@%d aliases=%s rt=%s",
                        snap.state, snap.side, LstCsvLog.f(snap.dominantLevelPx),
                        LstCsvLog.f(snap.domVsMed), LstCsvLog.f(snap.trend5mPts),
                        tracker.diagNqTrades(), tracker.diagEsTrades(), tracker.nqBookLevels(),
                        tracker.diagBookSnapCount(),
                        tracker.diagLastNqBurstSide(), tracker.diagLastNqBurstNs(),
                        tracker.diagLastEsBurstSide(), tracker.diagLastEsBurstNs(),
                        aliasesSeen, realtimeStarted);
                signalLog.writeRow(String.format(Locale.US,
                        "DIAG,%s,%d,%d,,,,,,,,,,,DIAG,%s,%s",
                        formatTime(nowNs), nowNs, snap.side, reason, realtimeStarted));
                if (tracker.diagNqTrades() > 2000 && tracker.diagEsTrades() == 0 && !warnedNoEs) {
                    warnedNoEs = true;
                    Log.warn("LST-AT NO ES TAPE — open an ES chart (not MNQ). "
                            + "Cross-confirm can never fire. aliases=" + aliasesSeen);
                    signalLog.writeRow(String.format(Locale.US,
                            "DIAG,%s,%d,0,,,,,,,,,,,DIAG,no_es_tape_open_ES_chart,%s",
                            formatTime(nowNs), nowNs, realtimeStarted));
                }
                lastDiagState = snap.state;
            }
        }
        stateIndicator.addPoint(stateCode(snap.state));
        signalIndicator.addPoint(0);
        if (snap.state == State.STACK_ARMED || snap.state == State.SLICING) {
            armedLevel = snap.dominantLevelPx;
            armedStop = Double.isNaN(armedLevel) ? Double.NaN
                    : armedLevel - snap.side * 2.0;
        } else if (snap.state == State.IDLE) {
            armedLevel = Double.NaN;
            armedStop = Double.NaN;
        }
        levelMarker.addPoint(armedLevel);
        stopMarker.addPoint(armedStop);
        if (fireUntilNs > 0 && nowNs > fireUntilNs) {
            lastFireLong = Double.NaN;
            lastFireShort = Double.NaN;
            fireUntilNs = 0;
        }
        fireLongMarker.addPoint(lastFireLong);
        fireShortMarker.addPoint(lastFireShort);

        // Time-stop flatten
        if (openSide != 0 && pendingTimeStopNs > 0 && nowNs >= pendingTimeStopNs
                && Boolean.TRUE.equals(enableTrading) && realtimeStarted) {
            Log.info("LST-AT time-stop hit — flatten");
            flatten("time_stop");
            pendingTimeStopNs = 0;
        }
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        if (tracker == null || size <= 0) {
            return;
        }
        long ts = nowNs > 0 ? nowNs : System.nanoTime();
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        if (currentAlias != null) {
            aliasesSeen.add(currentAlias + "=" + role);
        }
        // Secondary NQ-classified alias (e.g. MNQ tab) must not pollute NQ tape.
        if ("NQ".equals(role) && primaryAlias != null && currentAlias != null
                && !currentAlias.equals(primaryAlias)) {
            if (!warnedSecondaryNq) {
                warnedSecondaryNq = true;
                Log.warn("LST-AT secondary NQ alias ignored alias=" + currentAlias
                        + " primary=" + primaryAlias + " (open ES instead of MNQ)");
            }
            return;
        }
        boolean buy = tradeInfo.isBidAggressor;
        double px = PriceTicks.toChartPoints(price, pips);
        if ("NQ".equals(role)) {
            tracker.onNqTrade(ts, px, size, buy);
        } else if ("ES".equals(role)) {
            tracker.onEsTrade(ts, px, size, buy);
        }
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        if (tracker == null) {
            return;
        }
        String role = roleByAlias.getOrDefault(currentAlias, "OTHER");
        if ("NQ".equals(role) && primaryAlias != null && currentAlias != null
                && !currentAlias.equals(primaryAlias)) {
            return;
        }
        if ("NQ".equals(role) || "ES".equals(role)) {
            tracker.onDepth(role, isBid, price, size);
        }
    }

    @Override
    public void onSignal(Signal signal) {
        if (signal.kind != SignalKind.MOMENTUM) {
            return;
        }
        long coolNs = (long) ((signalCooldownSec == null ? 30.0 : signalCooldownSec) * 1e9);
        if (lastSignalNs > 0 && signal.signalTsNs - lastSignalNs < coolNs) {
            logSignal(signal, "SKIP", "cooldown", Double.NaN, Double.NaN);
            return;
        }
        if (signal.side > 0 && Boolean.FALSE.equals(allowLongs)) {
            logSignal(signal, "SKIP", "longs_disabled", Double.NaN, Double.NaN);
            return;
        }
        if (signal.side < 0 && Boolean.FALSE.equals(allowShorts)) {
            logSignal(signal, "SKIP", "shorts_disabled", Double.NaN, Double.NaN);
            return;
        }

        double tp = tpPts == null ? 16.0 : tpPts;
        double sl = resolveSlPts(signal);
        lastSignalNs = signal.signalTsNs;
        if (signal.side > 0) {
            lastFireLong = signal.entryRefPx;
            lastFireShort = Double.NaN;
        } else {
            lastFireShort = signal.entryRefPx;
            lastFireLong = Double.NaN;
        }
        fireUntilNs = nowNs + 8_000_000_000L;
        signalIndicator.addPoint(signal.side);

        boolean want = Boolean.TRUE.equals(enableTrading);
        boolean catchupOk = Boolean.TRUE.equals(ordersDuringCatchup);
        boolean can = want && api != null && (realtimeStarted || catchupOk);
        String action;
        if (can) {
            action = realtimeStarted ? "ORDER" : "ORDER_CATCHUP";
        } else if (want) {
            action = "SIGNAL_ONLY_WAIT_RT";
        } else {
            action = "SIGNAL_ONLY";
        }
        logSignal(signal, action, "ok", tp, sl);
        Log.info(String.format(Locale.US,
                "LST-AT FIRE %s side=%d px=%.2f level=%.2f stop=%.2f trend=%.1f dom=%.2f "
                        + "tp=%.1f sl=%.1f action=%s realtime=%s catchupOk=%s time=%s",
                signal.side > 0 ? "LONG" : "SHORT", signal.side, signal.entryRefPx,
                signal.dominantLevelPx, signal.stopPx, signal.trend5mPts, signal.domVsMed,
                tp, sl, action, realtimeStarted, catchupOk, formatTime(signal.signalTsNs)));

        if (can) {
            sendBracket(signal.side > 0, signal.entryRefPx, tp, sl, signal);
        }
    }

    private double resolveSlPts(Signal signal) {
        double min = slMinPts == null ? 4.0 : slMinPts;
        double max = slMaxPts == null ? 40.0 : slMaxPts;
        double raw = Math.abs(signal.entryRefPx - signal.stopPx);
        if (!Double.isFinite(raw) || raw <= 0) {
            raw = 10.0;
        }
        return Math.max(min, Math.min(max, raw));
    }

    private void logSignal(Signal s, String action, String reason, double tp, double sl) {
        if (signalLog == null) {
            return;
        }
        signalLog.writeRow(String.format(Locale.US,
                "SIGNAL,%s,%d,%d,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s",
                formatTime(s.signalTsNs), s.signalTsNs, s.side,
                LstCsvLog.f(s.entryRefPx), LstCsvLog.f(s.dominantLevelPx), LstCsvLog.f(s.stopPx),
                LstCsvLog.f(s.trend5mPts), LstCsvLog.f(s.domVsMed), LstCsvLog.f(s.burstTps),
                LstCsvLog.f(s.pullbackRatio), LstCsvLog.f(tp), LstCsvLog.f(sl),
                action, reason, Boolean.toString(realtimeStarted)));
    }

    private void sendBracket(boolean isBuy, double entryPx, double tp, double sl, Signal sig) {
        if (Boolean.TRUE.equals(flatOnly) && (flatCheckPosition() != 0 || flatLocked)) {
            logSignal(sig, "SKIP", "not_flat", tp, sl);
            Log.info("LST-AT ORDER blocked flat-only");
            return;
        }
        int size = Math.max(1, orderSize == null ? 1 : orderSize.intValue());
        double pip = pips > 0 ? pips : 0.25;
        int tpTicks = Math.max(1, (int) Math.round(tp / pip));
        int slTicks = Math.max(1, (int) Math.round(sl / pip));
        flatLocked = true;
        refreshTradingAccountId("order");
        try {
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias, isBuy, size, OrderDuration.GTC, null,
                    Double.NaN, Double.NaN, tpTicks, slTicks, 0, 0, false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            api.sendOrder(builder.build());
            openSide = isBuy ? 1 : -1;
            entryFillPx = entryPx;
            entryTsNs = sig.signalTsNs;
            openTpPts = tp;
            openSlPts = sl;
            openSignalId = Long.toString(sig.signalTsNs);
            double tstop = timeStopSec == null ? 0 : timeStopSec;
            pendingTimeStopNs = tstop > 0 ? nowNs + (long) (tstop * 1e9) : 0;
            Log.info(String.format(Locale.US,
                    "LST-AT ORDER sent %s size=%d entry~%.2f tp=%d sl=%d account=%s",
                    isBuy ? "BUY" : "SELL", size, entryPx, tpTicks, slTicks,
                    tradingAccountId == null ? "null" : tradingAccountId));
        } catch (Throwable t) {
            flatLocked = false;
            openSide = 0;
            Log.info("LST-AT ORDER failed: " + t);
        }
    }

    private void flatten(String reason) {
        if (api == null || openSide == 0) {
            return;
        }
        try {
            int size = Math.max(1, Math.abs(flatCheckPosition()));
            if (size == 0) {
                size = Math.max(1, orderSize == null ? 1 : orderSize.intValue());
            }
            boolean buyToCover = openSide < 0;
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias, buyToCover, size, OrderDuration.GTC, null,
                    Double.NaN, Double.NaN, 0, 0, 0, 0, false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            api.sendOrder(builder.build());
            Log.info("LST-AT FLATTEN reason=" + reason);
        } catch (Throwable t) {
            Log.info("LST-AT FLATTEN failed: " + t);
        }
    }

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
        position = statusInfo.position;
        if (statusInfo.position == 0 && statusInfo.workingBuys == 0 && statusInfo.workingSells == 0) {
            flatLocked = false;
            netFills = 0;
            openSide = 0;
            entryFillPx = 0;
            pendingTimeStopNs = 0;
        } else {
            flatLocked = true;
            if (statusInfo.position != 0) {
                netFills = statusInfo.position;
            }
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

        if (openSide == 0) {
            return;
        }
        boolean entryDir = (order.isBuy && openSide > 0) || (!order.isBuy && openSide < 0);
        if (entryDir && (order.type == OrderType.MKT || order.fakeMarketOrder)) {
            double fillPx = resolveFillPx(order);
            if (fillPx > 0 && !Double.isNaN(fillPx)) {
                entryFillPx = fillPx;
            }
            if (tradeLog != null) {
                tradeLog.writeRow(String.format(Locale.US,
                        "OPEN,%s,%d,%d,%s,,,,%s,%s,%s",
                        formatTime(nowNs), nowNs, openSide, LstCsvLog.f(entryFillPx),
                        LstCsvLog.f(openTpPts), LstCsvLog.f(openSlPts), openSignalId));
            }
            return;
        }
        boolean exitDir = (order.isBuy && openSide < 0) || (!order.isBuy && openSide > 0);
        if (exitDir) {
            double exitPx = resolveFillPx(order);
            double pnl = openSide * (exitPx - entryFillPx);
            String via = order.type == null ? "unknown" : order.type.toString();
            if (tradeLog != null) {
                tradeLog.writeRow(String.format(Locale.US,
                        "CLOSE,%s,%d,%d,%s,%s,%s,%s,%s,%s,%s",
                        formatTime(nowNs), nowNs, openSide, LstCsvLog.f(entryFillPx),
                        LstCsvLog.f(exitPx), LstCsvLog.f(pnl),
                        LstCsvLog.f(openTpPts), LstCsvLog.f(openSlPts), via, openSignalId));
            }
            Log.info(String.format(Locale.US,
                    "LST-AT TRADE CLOSED %s entry=%.2f exit=%.2f pnl=%.2f via=%s",
                    openSide > 0 ? "LONG" : "SHORT", entryFillPx, exitPx, pnl, via));
            openSide = 0;
            entryFillPx = 0;
            pendingTimeStopNs = 0;
        }
    }

    @Override
    public void onOrderExecuted(velox.api.layer1.data.ExecutionInfo executionInfo) {
        // fills via onOrderUpdated
    }

    private double resolveFillPx(OrderInfoUpdate order) {
        double pip = pips > 0 ? pips : 0.25;
        double avg = order.averageFillPrice;
        if (!Double.isNaN(avg) && avg > 0) {
            return avg >= 80_000 ? avg * pip : avg;
        }
        return entryFillPx;
    }

    @Override
    public void onState(State state, long tsNs) {
        // interval paint
    }

    private static int stateCode(State s) {
        switch (s) {
            case STACK_ARMED: return 1;
            case SLICING: return 2;
            case CLEARED_LEFT_BEHIND: return 3;
            case ABSORBED: return -1;
            default: return 0;
        }
    }

    private String formatTime(long tsNs) {
        if (tsNs <= 0) {
            return "";
        }
        return Instant.ofEpochSecond(tsNs / 1_000_000_000L, tsNs % 1_000_000_000L)
                .atZone(UK).format(TIME_FMT);
    }

    @Override
    public void stop() {
        if (signalLog != null) {
            signalLog.close();
        }
        if (tradeLog != null) {
            tradeLog.close();
        }
        if (provider != null && adminListener != null) {
            try {
                provider.removeListener(adminListener);
            } catch (Throwable ignored) {
            }
        }
    }
}
