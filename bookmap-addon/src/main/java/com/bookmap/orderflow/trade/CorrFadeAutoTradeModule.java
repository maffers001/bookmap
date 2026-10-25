package com.bookmap.orderflow.trade;

import java.awt.Color;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import com.bookmap.orderflow.core.CorrFadeDetector;
import com.bookmap.orderflow.core.CorrFadeDetector.FadeEvent;
import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.MatOpenVolAnchor;
import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.RegimeTracker;
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
 * Correlated Fade AutoTrade — H29 / EXP-0036_DIG4 frozen test auto-trade.
 *
 * <p>Signal (causal, 100 ms tape bars; see {@link CorrFadeDetector}):
 * ES+NQ <b>confirm</b> burst → first complete bar (≤ 5 s) where NQ still
 * bursts with the trade but ES is against it (side×imb ≤ −0.4 with prints) →
 * <b>fade</b> the burst at that bar's close.
 *
 * <p>Gates: UK 14:30–17:00, 60-min efficiency ratio ≤ 0.30, flat-only, 5 s
 * cooldown. Brackets (vol RR, {@code results/CFADE_ASYM_RR/}): unit =
 * k × median 30-min minute range; SL = clip(1R×unit, 4, 40), TP = clip(2R×unit, 4, 40).
 * <b>Open-vol</b> (same as MAT v4): for the first {@code Open vol period min}
 * after UK window open, unit uses the <em>prior-session</em> median 1-min
 * range (shared store with MAT); if none is stored, fixed open brackets
 * (TP=SL). After the open period, live vol RR as above. No trend / VWAP gate
 * (DIG3: those hurt the fade).
 *
 * <p>Separate from Momentum AutoTrade (continuation). Both can run together
 * as independent books (DIG3 §6: additive), but do <b>not</b> enable trading
 * on both against the same account until the fade has its own live record.
 *
 * <p>Attach on NQ with ES also open. Default: signals only
 * ({@code enableTrading=false}). Spec:
 * {@code results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md}.
 */
@Layer1SimpleAttachable
@Layer1TradingStrategy
@Layer1MultiAccountTradingSupported
@Layer1StrategyName("Correlated Fade AutoTrade (ES+NQ)")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class CorrFadeAutoTradeModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        PositionListener,
        OrdersListener,
        HistoricalModeListener {

    // --- Burst (EXP-0008 / MAT, confirm stamp) ---
    @Parameter(name = "Bar ms", step = 50.0)
    Double barMs = 100.0;

    @Parameter(name = "Burst min trades / bar", step = 1.0)
    Double minTrades = 6.0;

    @Parameter(name = "Burst min volume / bar", step = 1.0)
    Double minVol = 25.0;

    @Parameter(name = "Burst min |imbalance|", step = 0.05)
    Double minImbalance = 0.55;

    @Parameter(name = "Burst min progress (price pts)", step = 0.25)
    Double minProgressPts = 0.25;

    @Parameter(name = "Burst cooldown bars", step = 1.0)
    Double cooldownBars = 3.0;

    @Parameter(name = "ES sync window ms", step = 50.0)
    Double syncMs = 1500.0;

    @Parameter(name = "Max ES lag ms", step = 50.0)
    Double maxEsLagMs = 250.0;

    // --- Exhaustion / fade trigger (against_m04) ---
    @Parameter(name = "Fade max wait sec", step = 0.5)
    Double fadeMaxWaitSec = 5.0;

    @Parameter(name = "NQ still: min side×imb", step = 0.05)
    Double nqStillMinImb = 0.55;

    @Parameter(name = "NQ still: min volume", step = 1.0)
    Double nqStillMinVol = 25.0;

    @Parameter(name = "NQ still: min trades", step = 1.0)
    Double nqStillMinTrades = 6.0;

    /**
     * Frozen: 0.4. Bookmap spinners disallow negative defaults, so this is the
     * positive magnitude; the detector uses {@code −esAgainstImbMag}
     * (i.e. side×es_imb ≤ −0.4 on a bar with ES prints).
     */
    @Parameter(name = "ES against: min |side×imb|", step = 0.05, minimum = 0.0, maximum = 1.0)
    Double esAgainstImbMag = 0.4;

    @Parameter(name = "ES against: min volume (0=off)", step = 1.0)
    Double esAgainstMinVol = 0.0;

    // --- Gates (frozen) ---
    @Parameter(name = "Window start UK min-of-day", step = 5.0)
    Double windowStartUkMin = 870.0; // 14:30

    @Parameter(name = "Window end UK min-of-day", step = 5.0)
    Double windowEndUkMin = 1020.0; // 17:00

    @Parameter(name = "Regime window min", step = 1.0)
    Double regimeWindowMin = 60.0;

    /** Frozen: 0.30. Fade only when |net|/path over the regime window ≤ this. */
    @Parameter(name = "Max efficiency ratio (0=off)", step = 0.05)
    Double maxEr = 0.30;

    /**
     * Research regime bars start at the UK window open (14:30). When true the
     * regime tracker is cleared at window open so the first hour matches.
     */
    @Parameter(name = "Regime resets at window open")
    Boolean regimeResetAtWindowOpen = true;

    @Parameter(name = "Allow longs")
    Boolean allowLongs = true;

    @Parameter(name = "Allow shorts")
    Boolean allowShorts = true;

    @Parameter(name = "Signal cooldown sec", step = 1.0)
    Double signalCooldownSec = 5.0;

    @Parameter(name = "Flat-only (no pyramid)")
    Boolean flatOnly = true;

    // --- Brackets (vol RR: SL 1R / TP 2R — CFADE_ASYM_RR) ---
    @Parameter(name = "Vol multiple k", step = 0.5)
    Double volMultipleK = 1.0;

    @Parameter(name = "Vol window min", step = 1.0)
    Double volWindowMin = 30.0;

    @Parameter(name = "SL R multiple", step = 0.25)
    Double slRMultiple = 1.0;

    @Parameter(name = "TP R multiple", step = 0.25)
    Double tpRMultiple = 2.0;

    @Parameter(name = "Bracket min pts", step = 1.0)
    Double bracketMinPts = 4.0;

    @Parameter(name = "Bracket max pts", step = 1.0)
    Double bracketMaxPts = 40.0;

    /**
     * Same as MAT v4: during the first N minutes after UK window open, use
     * prior-session median for vol unit (or fixed open size if no prior).
     */
    @Parameter(name = "Open vol period min", step = 1.0)
    Double openVolPeriodMin = 20.0;

    /** Fixed TP=SL (pts) when in open period and no prior median is available. */
    @Parameter(name = "Open fixed bracket pts", step = 0.5)
    Double openFixedBracketPts = 10.0;

    @Parameter(name = "Enable open vol anchor")
    Boolean enableOpenVolAnchor = true;

    // --- Trading ---
    @Parameter(name = "Enable trading (OFF for signal-only)")
    Boolean enableTrading = false;

    @Parameter(name = "Use selected account (dropdown)")
    Boolean useSelectedAccount = true;

    @Parameter(name = "Order size", step = 1.0)
    Double orderSize = 1.0;

    /** Spec: stop for the session when realised net ≤ −150 pts. 0 = off. */
    @Parameter(name = "Kill switch: max session loss pts (0=off)", step = 10.0)
    Double killMaxSessionLossPts = 150.0;

    @Parameter(name = "Verbose log (bursts + every fade candidate)")
    Boolean verboseLog = true;

    // --- runtime ---
    private Api api;
    private Layer1ApiProvider provider;
    private Layer1ApiAdminAdapter adminListener;
    private final AccountListManager accountListManager = new AccountListManager();
    private String primaryAlias;
    private String currentAlias;
    private long nowNs;
    private long lastSeenTsNs = -1;
    private double pips = 0.25;
    private double esPips = 0.25;
    private volatile boolean realtimeStarted;
    /** First few tape prints after realtime — verify aggressor polarity vs extract. */
    private int aggressorSampleLeft = 8;
    /** Warn once if a secondary NQ alias prints (research/live both use primary only). */
    private boolean warnedSecondaryNq;

    private final Map<String, InstrumentState> byAlias = new HashMap<>();
    private CorrFadeDetector detector;
    private final RegimeTracker regime = new RegimeTracker();
    private boolean windowMarked;
    private LocalDate windowMarkedDay;

    private Indicator signalIndicator;
    private Indicator longMarker;
    private Indicator shortMarker;
    private Indicator erIndicator;
    private Indicator pendingIndicator;
    private boolean signalThisInterval;

    private long lastSignalNs;
    private int lastSignalSide;
    private long fadeCandidateCount;
    private long signalCount;

    /** Prior-session median 1-min range for open-vol brackets (MAT-shared store). */
    private double priorVolMedian = Double.NaN;
    private boolean priorVolLoaded;
    private long lastPriorPersistNs;

    // position / orders
    private int position;
    private int netFills;
    private boolean flatLocked;
    private final Map<String, Integer> filledByOrderId = new HashMap<>();
    private volatile String tradingAccountId;
    private int openSide;
    private double entryFillPx;

    // kill switch / session stats
    private double sessionNetPts;
    private int sessionTrades;
    private int sessionWins;
    private int consecutiveLosses;
    private boolean tradingHalted;
    private LocalDate sessionDateUk;

    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final DateTimeFormatter TIME_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS z");

    private static final class InstrumentState {
        String role = "OTHER";
        double instrumentPips = 0.25;
        double lastPrice;
    }

    // ------------------------------------------------------------ lifecycle

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
        this.openSide = 0;
        this.entryFillPx = 0;
        this.lastSignalNs = 0;
        this.windowMarked = false;
        this.regime.reset();
        resetSessionStats("init");

        InstrumentState s = new InstrumentState();
        s.role = InstrumentClassifier.classify(info);
        s.instrumentPips = pips;
        byAlias.put(alias, s);
        rebuildDetector("init");

        signalIndicator = api.registerIndicator("CF Signal", GraphType.BOTTOM);
        erIndicator = api.registerIndicator("CF ER60", GraphType.BOTTOM);
        pendingIndicator = api.registerIndicator("CF Pending fades", GraphType.BOTTOM);
        longMarker = api.registerIndicator("CF Fade Long", GraphType.PRIMARY);
        shortMarker = api.registerIndicator("CF Fade Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 160, 40));
        erIndicator.setColor(new Color(180, 180, 255));
        pendingIndicator.setColor(new Color(140, 140, 140));
        longMarker.setColor(new Color(40, 220, 160));
        shortMarker.setColor(new Color(240, 90, 160));
        longMarker.setWidth(3);
        shortMarker.setWidth(3);

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(erIndicator);
        bottom.add(pendingIndicator);

        installAccountListener(api);
        refreshTradingAccountId("init");

        Log.info("CFADE init on " + alias
                + " role=" + s.role
                + " trading=" + Boolean.TRUE.equals(enableTrading)
                + " longs=" + Boolean.TRUE.equals(allowLongs)
                + " shorts=" + Boolean.TRUE.equals(allowShorts)
                + " pips=" + pips
                + " esAgainstImbMag=" + esAgainstImbMag
                + " maxEr=" + maxEr
                + " fadeMaxWaitSec=" + fadeMaxWaitSec
                + " volK=" + volMultipleK
                + " slR=" + slRMultiple
                + " tpR=" + tpRMultiple
                + " floor=" + bracketMinPts
                + " cap=" + bracketMaxPts
                + " openVol=" + Boolean.TRUE.equals(enableOpenVolAnchor)
                + " openPeriodMin=" + openVolPeriodMin
                + " openFixed=" + openFixedBracketPts
                + " window=" + windowStartUkMin + "-" + windowEndUkMin
                + " kill=" + killMaxSessionLossPts
                + " (H29 fade; vol RR sl1/tp2 + open-vol; spec results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md)");
    }

    private static final class BracketPts {
        final double tp;
        final double sl;
        /** {@code prior} / {@code fixed} / {@code live} — open-vol path taken. */
        final String volSrc;

        BracketPts(double tp, double sl, String volSrc) {
            this.tp = tp;
            this.sl = sl;
            this.volSrc = volSrc == null ? "live" : volSrc;
        }
    }

    private void rebuildDetector(String via) {
        CorrFadeDetector.Config c = new CorrFadeDetector.Config();
        c.barMs = barMs == null ? 100 : Math.max(10, barMs.intValue());
        c.minTrades = minTrades == null ? 6 : minTrades.intValue();
        c.minVol = minVol == null ? 25.0 : minVol;
        c.minImbalance = minImbalance == null ? 0.55 : minImbalance;
        double progPts = minProgressPts == null ? 0.25 : minProgressPts;
        // API trade prices are tick indices → progress threshold in ticks.
        c.nqMinProgress = PriceTicks.pointsToApi(progPts, pips, true);
        c.esMinProgress = PriceTicks.pointsToApi(progPts, esPips, true);
        c.cooldownBars = cooldownBars == null ? 3 : cooldownBars.intValue();
        c.syncMs = syncMs == null ? 1500.0 : syncMs;
        c.maxEsLagMs = maxEsLagMs == null ? 250.0 : maxEsLagMs;
        c.fadeMaxWaitSec = fadeMaxWaitSec == null ? 5.0 : fadeMaxWaitSec;
        c.nqStillMinImb = nqStillMinImb == null ? 0.55 : nqStillMinImb;
        c.nqStillMinVol = nqStillMinVol == null ? 25.0 : nqStillMinVol;
        c.nqStillMinTrades = nqStillMinTrades == null ? 6.0 : nqStillMinTrades;
        // Panel is positive magnitude; detector threshold is −mag (side×es_imb ≤ −0.4).
        double mag = esAgainstImbMag == null ? 0.4 : Math.max(0.0, esAgainstImbMag);
        c.esAgainstImbMax = -mag;
        c.esAgainstMinVol = esAgainstMinVol == null ? 0.0 : esAgainstMinVol;
        detector = new CorrFadeDetector(c, new CorrFadeDetector.Listener() {
            @Override
            public void onNqBurst(Burst b, boolean pendingEs) {
                if (Boolean.TRUE.equals(verboseLog) && pendingEs) {
                    Log.info(String.format("CFADE NQ burst#%d side=%d vol=%.0f imb=%.2f pending ES confirm %s",
                            detector.nqBurstCount(), b.side, b.totalVol, b.imbalance, formatTime(b.tsNs)));
                }
            }

            @Override
            public void onEsBurst(Burst b) {
                long n = detector.esBurstCount();
                if (Boolean.TRUE.equals(verboseLog) && (n <= 5 || n % 50 == 0)) {
                    Log.info(String.format("CFADE ES burst#%d side=%d n=%d vol=%.0f imb=%.2f %s",
                            n, b.side, b.nTrades, b.totalVol, b.imbalance, formatTime(b.tsNs)));
                }
            }

            @Override
            public void onConfirm(int side, long stampNs, long nqTsNs, long esTsNs, long lagMs) {
                if (Boolean.TRUE.equals(verboseLog)) {
                    Log.info(String.format("CFADE CONFIRM#%d side=%d stamp=%s lagMs=%d nqTs=%d esTs=%d",
                            detector.confirmCount(), side, formatTime(stampNs), lagMs, nqTsNs, esTsNs));
                }
            }

            @Override
            public void onExpire(CorrFadeDetector.ExpireDetail d) {
                if (!Boolean.TRUE.equals(verboseLog)) {
                    return;
                }
                Log.info(String.format(
                        "CFADE EXPIRE side=%d stamp=%s slots=%d nqPrint=%d esPrint=%d "
                                + "nqStillHits=%d esAgainstHits=%d bestSide*nqImb=%s bestSide*esImb=%s "
                                + "(need nqStill>=%.2f and side*esImb<=%.2f)",
                        d.burstSide, formatTime(d.stampNs), d.slotsChecked, d.nqPrintSlots, d.esPrintSlots,
                        d.nqStillHits, d.esAgainstHits,
                        fmtOpt(d.bestNqSideImb), fmtOpt(d.bestEsSideImb),
                        c.nqStillMinImb, c.esAgainstImbMax));
            }

            @Override
            public void onSlotProbe(CorrFadeDetector.SlotProbe p) {
                if (!Boolean.TRUE.equals(verboseLog)) {
                    return;
                }
                // nqStill without esAgainst is the usual miss; also log the reverse.
                String why = p.nqStill && !p.esAgainst
                        ? (p.esPresent
                        ? String.format("ES_not_against side*esImb=%.2f (need<=%.2f)", p.sideEsImb, c.esAgainstImbMax)
                        : "ES_no_prints")
                        : String.format("NQ_not_still side*nqImb=%s n=%d vol=%.0f",
                        fmtOpt(p.sideNqImb), p.nqTrades, p.nqVol);
                Log.info(String.format(
                        "CFADE PROBE confirm=%s slot=%s burstSide=%+d %s | "
                                + "nqStill=%s nq_imb=%s nq_vol=%.0f nq_n=%d | "
                                + "esAgainst=%s es_imb=%s es_vol=%.0f",
                        formatTime(p.confirmStampNs), formatTime(p.slotNs), p.burstSide, why,
                        p.nqStill, fmtOpt(p.nqImb), p.nqVol, p.nqTrades,
                        p.esAgainst, fmtOpt(p.esImb), p.esVol));
            }

            @Override
            public void onFade(FadeEvent e) {
                onFadeEvent(e);
            }
        });
        Log.info(String.format(
                "CFADE detector built (%s) bar=%dms nqProg=%.2f esProg=%.2f esImbMax=%.2f wait=%.1fs "
                        + "slotReady=dualWatermark+1barGrace",
                via, c.barMs, c.nqMinProgress, c.esMinProgress, c.esAgainstImbMax, c.fadeMaxWaitSec));
        double floor = bracketMinPts == null ? 4.0 : bracketMinPts;
        double cap = bracketMaxPts == null ? 40.0 : bracketMaxPts;
        if (floor != 4.0 || cap != 40.0) {
            Log.info(String.format(
                    "CFADE WARN bracket clip floor=%.1f cap=%.1f (live default / backtest sl1tp2 uses 4–40)",
                    floor, cap));
        }
    }

    private static String fmtOpt(double v) {
        return Double.isNaN(v) ? "na" : String.format("%.2f", v);
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : info.symbol;
        double ipips = info.pips > 0 ? info.pips : 0.25;
        InstrumentState state = byAlias.computeIfAbsent(alias, a -> new InstrumentState());
        String role = InstrumentClassifier.classify(info);
        state.role = role;
        state.instrumentPips = ipips;
        Log.info(String.format("CFADE instrument added alias=%s symbol=%s role=%s pips=%.4f",
                alias, info.symbol, role, ipips));
        boolean rebuild = false;
        if (alias.equals(primaryAlias) && info.pips > 0 && info.pips != pips) {
            pips = info.pips;
            rebuild = true;
        }
        if ("ES".equals(role) && info.pips > 0 && info.pips != esPips) {
            esPips = info.pips;
            rebuild = true;
        }
        if (rebuild) {
            rebuildDetector("instrument_added");
        }
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        byAlias.computeIfAbsent(alias, a -> new InstrumentState());
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        if (lastSeenTsNs > 0 && nanoseconds + 500_000_000L < lastSeenTsNs) {
            Log.info(String.format("CFADE rewind detected prevTs=%d newTs=%d — clearing state",
                    lastSeenTsNs, nanoseconds));
            detector.reset();
            regime.reset();
            windowMarked = false;
            lastSignalNs = 0;
        }
        this.nowNs = nanoseconds;
        this.lastSeenTsNs = nanoseconds;
        maybeRollSessionDate(nanoseconds);
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        lastSignalNs = 0;
        flatLocked = false;
        aggressorSampleLeft = 8;
        refreshTradingAccountId("realtime");
        Log.info(String.format(
                "CFADE realtime start — orders %s account=%s pos=%d halted=%s sessionNet=%.2f",
                Boolean.TRUE.equals(enableTrading) ? "ENABLED" : "disabled (signal-only)",
                tradingAccountId == null ? "null(primary)" : tradingAccountId,
                position, tradingHalted, sessionNetPts));
    }

    @Override
    public void stop() {
        if (provider != null && adminListener != null) {
            try {
                provider.removeListener(adminListener);
            } catch (Throwable t) {
                Log.info("CFADE account listener remove failed: " + t);
            }
        }
        byAlias.clear();
        Log.info(String.format(
                "CFADE stopped. bursts nq=%d es=%d confirmed=%d fades=%d candidates=%d signals=%d "
                        + "sessionTrades=%d wins=%d net=%.2f halted=%s",
                detector.nqBurstCount(), detector.esBurstCount(), detector.confirmCount(),
                detector.fadeCount(), fadeCandidateCount, signalCount,
                sessionTrades, sessionWins, sessionNetPts, tradingHalted));
    }

    // ------------------------------------------------------------ data

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        InstrumentState state = byAlias.computeIfAbsent(currentAlias, a -> new InstrumentState());
        state.lastPrice = price;
        long ts = nowNs > 0 ? nowNs : System.nanoTime();
        // Bookmap: isBidAggressor true ⇒ aggressive buy (matches SessionExtractor is_buy_aggressor).
        boolean buyAgg = tradeInfo.isBidAggressor;

        if (realtimeStarted && aggressorSampleLeft > 0
                && ("ES".equals(state.role) || isPrimaryNq(state))) {
            aggressorSampleLeft--;
            double chartPx = PriceTicks.toChartPoints(price, state.instrumentPips);
            Log.info(String.format(
                    "CFADE AGG sample role=%s isBidAggressor=%s → buyAgg=%s pxApi=%.2f pxChart=%.2f sz=%d time=%s",
                    state.role, tradeInfo.isBidAggressor, buyAgg, price, chartPx, size, formatTime(ts)));
        }

        if ("ES".equals(state.role)) {
            detector.onEsTrade(ts, price, size, buyAgg);
            return;
        }
        if ("NQ".equals(state.role) && currentAlias != null && primaryAlias != null
                && !currentAlias.equals(primaryAlias)) {
            if (!warnedSecondaryNq) {
                warnedSecondaryNq = true;
                Log.info(String.format(
                        "CFADE NQ secondary alias ignored alias=%s primary=%s "
                                + "(single-feed parity with research; do not merge TV+Rithmic tape)",
                        currentAlias, primaryAlias));
            }
            return;
        }
        if (!isPrimaryNq(state)) {
            return;
        }
        double chartPx = PriceTicks.toChartPoints(price, state.instrumentPips);
        maybeMarkWindow(ts);
        regime.onTrade(ts, chartPx, size);
        detector.onNqTrade(ts, price, size, buyAgg);
    }

    private boolean isPrimaryNq(InstrumentState state) {
        return currentAlias != null && currentAlias.equals(primaryAlias)
                && ("NQ".equals(state.role) || "OTHER".equals(state.role));
    }

    private InstrumentState primaryState() {
        return primaryAlias == null ? null : byAlias.get(primaryAlias);
    }

    /** Regime / VWAP anchor at UK window open (matches research 14:30 cut). */
    private void maybeMarkWindow(long tsNs) {
        LocalDate day = ukDate(tsNs);
        if (windowMarked && day.equals(windowMarkedDay)) {
            return;
        }
        int start = windowStartUkMin == null ? 870 : windowStartUkMin.intValue();
        if (ukMinuteOfDay(tsNs) >= start) {
            if (Boolean.TRUE.equals(regimeResetAtWindowOpen)) {
                regime.reset();
            }
            regime.markSessionOpen(tsNs);
            windowMarked = true;
            windowMarkedDay = day;
            Log.info("CFADE window open marked " + formatTime(tsNs)
                    + " regimeReset=" + Boolean.TRUE.equals(regimeResetAtWindowOpen));
        }
    }

    // ------------------------------------------------------------ fade event → gates → signal

    private static final class Eval {
        final boolean ok;
        final String reason;

        Eval(boolean ok, String reason) {
            this.ok = ok;
            this.reason = reason;
        }
    }

    private void onFadeEvent(FadeEvent e) {
        fadeCandidateCount++;
        InstrumentState nq = primaryState();
        double ipips = nq != null ? nq.instrumentPips : pips;
        int rw = regimeWindowMin == null ? 60 : Math.max(1, regimeWindowMin.intValue());
        RegimeTracker.Snapshot sr = regime.snapshot(rw, e.stampNs);
        double chartPx = PriceTicks.toChartPoints(e.nqClose, ipips);

        Eval ev = evaluateGates(e.side, e.stampNs, sr);

        String tag = ev.ok ? "CFADE FADE PASS" : "CFADE FADE REJECT";
        if (Boolean.TRUE.equals(verboseLog) || ev.ok || ev.reason.startsWith("regime_")) {
            Log.info(String.format(
                    "%s reason=%s side=%s px=%.2f time=%s tsNs=%d burstSide=%d wait_ms=%.0f lagMs=%d detectDelayMs=%.0f "
                            + "nq_imb_x=%.2f nq_vol_x=%.0f es_imb_x=%.2f es_vol_x=%.0f "
                            + "er%d=%.3f range%d=%.1f nBars=%d burst_vol=%.0f burst_imb=%.2f "
                            + "flatPos=%d locked=%s halted=%s",
                    tag, ev.reason, e.side > 0 ? "LONG" : "SHORT", chartPx, formatTime(e.stampNs), e.stampNs,
                    e.burstSide, e.waitMs, e.lagMs, e.detectDelayMs,
                    e.nqImbX, e.nqVolX, e.esImbX, e.esVolX,
                    rw, sr.efficiencyRatio, rw, sr.rangePts, sr.nBars, e.burstVol, e.burstImb,
                    flatCheckPosition(), flatLocked, tradingHalted));
        }
        erIndicator.addPoint(sr.efficiencyRatio);
        if (!ev.ok) {
            return;
        }
        fireSignal(e, chartPx, sr);
    }

    private Eval evaluateGates(int side, long tsNs, RegimeTracker.Snapshot sr) {
        int mod = ukMinuteOfDay(tsNs);
        int start = windowStartUkMin == null ? 870 : windowStartUkMin.intValue();
        int end = windowEndUkMin == null ? 1020 : windowEndUkMin.intValue();
        if (mod < start || mod >= end) {
            return new Eval(false, "outside_window");
        }
        if (side > 0 && !Boolean.TRUE.equals(allowLongs)) {
            return new Eval(false, "longs_disabled");
        }
        if (side < 0 && !Boolean.TRUE.equals(allowShorts)) {
            return new Eval(false, "shorts_disabled");
        }
        if (Boolean.TRUE.equals(flatOnly)) {
            int pos = flatCheckPosition();
            if (pos != 0 || flatLocked) {
                return new Eval(false, "not_flat(pos=" + pos + ",locked=" + flatLocked + ")");
            }
        }
        long coolNs = (long) ((signalCooldownSec == null ? 5.0 : signalCooldownSec) * 1_000_000_000.0);
        if (lastSignalNs > 0 && tsNs - lastSignalNs < coolNs) {
            return new Eval(false, "signal_cooldown");
        }
        double er = maxEr == null ? 0.0 : maxEr;
        if (er > 0 && sr.efficiencyRatio > er) {
            return new Eval(false, "regime_er_skip");
        }
        return new Eval(true, "ok");
    }

    /**
     * Vol RR brackets matching {@code make_vol_bracket_rr(k, tp_r, sl_r)}:
     * unit = k × median minute range; SL = clip(slR×unit), TP = clip(tpR×unit).
     * Defaults: k=1, slR=1, tpR=2, floor=4, cap=40 ({@code CFADE_ASYM_RR}).
     *
     * <p>Open-vol (MAT v4): in the first {@code openVolPeriodMin} after UK
     * window open, median is the prior-session file (shared with MAT); if
     * missing, TP=SL=fixed open size. After the open period, live median.
     */
    private BracketPts resolveBracketPts(long asofNs) {
        double k = volMultipleK == null ? 1.0 : volMultipleK;
        double slR = slRMultiple == null ? 1.0 : slRMultiple;
        double tpR = tpRMultiple == null ? 2.0 : tpRMultiple;
        double floor = bracketMinPts == null ? 4.0 : bracketMinPts;
        double cap = bracketMaxPts == null ? 40.0 : bracketMaxPts;

        int openMin = windowStartUkMin == null ? 870 : windowStartUkMin.intValue();
        int openPeriod = openVolPeriodMin == null ? 20 : Math.max(1, openVolPeriodMin.intValue());
        boolean openAnchor = Boolean.TRUE.equals(enableOpenVolAnchor)
                && openMin > 0
                && asofNs > 0
                && MatOpenVolAnchor.inOpenPeriod(asofNs, openMin, openPeriod);

        String volSrc;
        double med;
        if (openAnchor) {
            ensurePriorVolLoaded();
            if (Double.isFinite(priorVolMedian) && priorVolMedian > 0) {
                med = priorVolMedian;
                volSrc = "prior";
            } else {
                double fixed = openFixedBracketPts == null ? 10.0 : openFixedBracketPts;
                double pts = Math.max(floor, Math.min(cap, fixed));
                return new BracketPts(pts, pts, "fixed");
            }
        } else {
            int vw = volWindowMin == null ? 30 : Math.max(1, volWindowMin.intValue());
            RegimeTracker.Snapshot sv = regime.snapshot(vw, asofNs);
            med = sv.medianMinuteRange;
            volSrc = "live";
        }
        double unit = k * Math.max(med, 0.25);
        double sl = Math.max(floor, Math.min(cap, slR * unit));
        double tp = Math.max(floor, Math.min(cap, tpR * unit));
        return new BracketPts(tp, sl, volSrc);
    }

    private void ensurePriorVolLoaded() {
        if (priorVolLoaded) {
            return;
        }
        priorVolMedian = MatOpenVolAnchor.loadPriorMedian();
        priorVolLoaded = true;
        if (Double.isFinite(priorVolMedian)) {
            Log.info(String.format("CFADE open-vol: loaded prior median=%.2f from %s",
                    priorVolMedian, MatOpenVolAnchor.storePath()));
        } else {
            Log.info("CFADE open-vol: no prior median; open period will use fixed brackets");
        }
    }

    /** Persist today's post-open session median for tomorrow's open anchor. */
    private void maybePersistPriorVol() {
        if (!Boolean.TRUE.equals(enableOpenVolAnchor)) {
            return;
        }
        long asof = nowNs > 0 ? nowNs : 0;
        if (asof <= 0) {
            return;
        }
        if (lastPriorPersistNs > 0 && asof - lastPriorPersistNs < 60_000_000_000L) {
            return;
        }
        int openMin = windowStartUkMin == null ? 870 : windowStartUkMin.intValue();
        int openPeriod = openVolPeriodMin == null ? 20 : Math.max(1, openVolPeriodMin.intValue());
        if (openMin > 0 && MatOpenVolAnchor.inOpenPeriod(asof, openMin, openPeriod)) {
            return; // still in open — do not overwrite prior with thin tape
        }
        int minBars = volWindowMin == null ? 30 : Math.max(15, volWindowMin.intValue() / 2);
        int n = regime.sessionBarsCount(asof);
        if (n < minBars) {
            return;
        }
        double med = regime.medianRangeSinceSessionOpen(asof, minBars);
        if (!Double.isFinite(med) || med <= 0) {
            return;
        }
        MatOpenVolAnchor.savePriorMedian(med, n);
        lastPriorPersistNs = asof;
    }

    private void fireSignal(FadeEvent e, double chartPx, RegimeTracker.Snapshot sr) {
        int side = e.side;
        signalCount++;
        lastSignalNs = e.stampNs;
        lastSignalSide = side;
        signalThisInterval = true;
        signalIndicator.addPoint(side);
        if (side > 0) {
            longMarker.addPoint(chartPx);
            shortMarker.addPoint(Double.NaN);
        } else {
            shortMarker.addPoint(chartPx);
            longMarker.addPoint(Double.NaN);
        }
        BracketPts brkt = resolveBracketPts(e.stampNs);
        boolean wantTrade = Boolean.TRUE.equals(enableTrading);
        boolean willTrade = wantTrade && realtimeStarted && api != null && !tradingHalted;
        Log.info(String.format(
                "CFADE SIGNAL %s @%.2f time=%s tsNs=%d wait_ms=%.0f es_imb_x=%.2f er60=%.3f "
                        + "tp=%.1f sl=%.1f volSrc=%s trading=%s realtime=%s halted=%s pos=%d account=%s sessionNet=%.2f",
                side > 0 ? "LONG" : "SHORT", chartPx, formatTime(e.stampNs), e.stampNs, e.waitMs, e.esImbX,
                sr.efficiencyRatio, brkt.tp, brkt.sl, brkt.volSrc, wantTrade, realtimeStarted, tradingHalted,
                flatCheckPosition(), tradingAccountId == null ? "null" : tradingAccountId, sessionNetPts));
        if (willTrade) {
            sendBracketOrder(side > 0, chartPx, brkt);
        } else if (wantTrade && tradingHalted) {
            Log.info("CFADE ORDER skipped — kill switch active for this session");
        } else if (wantTrade && !realtimeStarted) {
            Log.info("CFADE ORDER skipped (catchup/history — waiting for realtime)");
        }
    }

    // ------------------------------------------------------------ interval

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        if (!signalThisInterval) {
            signalIndicator.addPoint(0);
        }
        signalThisInterval = false;
        if (nowNs > 0) {
            detector.onTime(nowNs);
            int rw = regimeWindowMin == null ? 60 : Math.max(1, regimeWindowMin.intValue());
            erIndicator.addPoint(regime.snapshot(rw, nowNs).efficiencyRatio);
            maybePersistPriorVol();
        }
        pendingIndicator.addPoint(detector.pendingFades());
    }

    // ------------------------------------------------------------ orders / position

    private int flatCheckPosition() {
        return position != 0 ? position : netFills;
    }

    private void sendBracketOrder(boolean isBuy, double entryPricePoints, BracketPts brkt) {
        int size = Math.max(1, orderSize == null ? 1 : orderSize.intValue());
        double pip = pips > 0 ? pips : 0.25;
        int tpTicks = Math.max(1, (int) Math.round(brkt.tp / pip));
        int slTicks = Math.max(1, (int) Math.round(brkt.sl / pip));

        if (Boolean.TRUE.equals(flatOnly) && (flatCheckPosition() != 0 || flatLocked)) {
            Log.info("CFADE ORDER blocked flat-only pos=" + position
                    + " netFills=" + netFills + " locked=" + flatLocked);
            return;
        }
        flatLocked = true;
        refreshTradingAccountId("order");
        try {
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias, isBuy, size, OrderDuration.GTC, null,
                    Double.NaN, Double.NaN, tpTicks, slTicks, 0, 0, false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            SimpleOrderSendParameters order = builder.build();
            api.sendOrder(order);
            openSide = isBuy ? 1 : -1;
            entryFillPx = entryPricePoints;
            Log.info(String.format(
                    "CFADE ORDER sent %s size=%d entry~%.2f tp=%d ticks (~%.2f pts) sl=%d ticks (~%.2f pts) "
                            + "account=%s time=%s",
                    isBuy ? "BUY" : "SELL", size, entryPricePoints, tpTicks, brkt.tp, slTicks, brkt.sl,
                    tradingAccountId == null ? "null(primary)" : tradingAccountId, formatTime(nowNs)));
        } catch (Throwable t) {
            flatLocked = false;
            openSide = 0;
            Log.info("CFADE ORDER failed: " + t);
        }
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
        } else {
            flatLocked = true;
            if (statusInfo.position != 0) {
                netFills = statusInfo.position;
            }
        }
        Log.info(String.format("CFADE POS pos=%d avg=%.2f wb=%d ws=%d unreal=%.2f locked=%s openSide=%d",
                statusInfo.position, statusInfo.averagePrice, statusInfo.workingBuys,
                statusInfo.workingSells, statusInfo.unrealizedPnl, flatLocked, openSide));
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
        // Replay SIM often leaves averageFillPrice=NaN; fall back to limit/stop/last.
        double fillPx = resolveFillPx(order);
        Log.info(String.format("CFADE FILL %s order=%s type=%s delta=%d filled=%d px=%.2f netFills=%d statusPos=%d",
                order.isBuy ? "BUY" : "SELL", order.orderId, order.type, delta, filled, fillPx,
                netFills, position));

        if (openSide == 0) {
            return;
        }
        boolean entryDir = (order.isBuy && openSide > 0) || (!order.isBuy && openSide < 0);
        if (entryDir) {
            if (order.type == OrderType.MKT || order.fakeMarketOrder) {
                // Keep signal entry when SIM omits fill price — never overwrite with NaN.
                if (fillPx > 0 && !Double.isNaN(fillPx)) {
                    entryFillPx = fillPx;
                }
                Log.info(String.format("CFADE trade open %s entry=%.2f",
                        openSide > 0 ? "LONG" : "SHORT", entryFillPx));
            }
            return;
        }
        // Opposite-direction fill = bracket leg (TP LMT / SL STP) closing the trade.
        if (!(fillPx > 0) || Double.isNaN(fillPx)) {
            Log.info("CFADE TRADE CLOSED skipped — no usable exit price (avg/limit/stop/last all missing)");
            return;
        }
        if (!(entryFillPx > 0) || Double.isNaN(entryFillPx)) {
            Log.info("CFADE TRADE CLOSED skipped — entry still unset/NaN");
            return;
        }
        onTradeClosed(fillPx, order.type);
    }

    /**
     * Replay SIM {@code averageFillPrice} is often NaN. Prefer avg when present;
     * else LMT {@code limitPrice} / STP {@code stopPrice}; else last tape price.
     *
     * <p>Order avg/limit/stop/execution prices on this install are already chart
     * points (e.g. 30785). Tape {@code onTrade} prices are tick indices and still
     * need {@link PriceTicks#toChartPoints}. Applying that helper to order prices
     * wrongly ×pips (30785→7696) and trips the kill switch.
     */
    private double resolveFillPx(OrderInfoUpdate order) {
        double pip = pips > 0 ? pips : 0.25;
        double avg = order.averageFillPrice;
        if (!Double.isNaN(avg) && avg > 0) {
            return orderPriceToChart(avg, pip);
        }
        if (order.type == OrderType.LMT || order.type == OrderType.STP_LMT) {
            double lim = order.limitPrice;
            if (!Double.isNaN(lim) && lim != 0) {
                return orderPriceToChart(lim, pip);
            }
        }
        if (order.type == OrderType.STP || order.type == OrderType.STP_LMT) {
            double stop = order.stopPrice;
            if (!Double.isNaN(stop) && stop != 0) {
                return orderPriceToChart(stop, pip);
            }
        }
        InstrumentState nq = primaryState();
        if (nq != null && nq.lastPrice > 0 && !Double.isNaN(nq.lastPrice)) {
            return PriceTicks.toChartPoints(nq.lastPrice, pip);
        }
        return Double.NaN;
    }

    /** Order-layer prices → chart points (usually already points; see resolveFillPx). */
    private double orderPriceToChart(double orderPx, double pip) {
        if (entryFillPx > 0 && !Double.isNaN(entryFillPx)
                && Math.abs(orderPx - entryFillPx) <= 500.0) {
            return orderPx; // already points near the signal entry
        }
        double asTicks = PriceTicks.toChartPoints(orderPx, pip);
        if (entryFillPx > 0 && !Double.isNaN(entryFillPx)
                && Math.abs(asTicks - entryFillPx) <= 500.0) {
            return asTicks; // was a tick index
        }
        // No entry yet (or ambiguous): NQ chart ~3e4, NQ ticks ~1e5.
        if (orderPx >= 80_000.0) {
            return orderPx * pip;
        }
        return orderPx;
    }

    private void onTradeClosed(double exitPx, OrderType type) {
        double pnl = openSide * (exitPx - entryFillPx);
        if (Double.isNaN(pnl)) {
            Log.info(String.format(
                    "CFADE TRADE CLOSED skipped NaN pnl entry=%.2f exit=%.2f via=%s",
                    entryFillPx, exitPx, type));
            openSide = 0;
            entryFillPx = 0;
            return;
        }
        // Guard: max TP/SL cap is 40 pts; abs(pnl)≫ that means a price-unit bug.
        if (Math.abs(pnl) > 100.0) {
            Log.info(String.format(
                    "CFADE TRADE CLOSED ignored absurd pnl=%.2f entry=%.2f exit=%.2f via=%s "
                            + "(price-unit mismatch?) — position cleared, sessionNet unchanged",
                    pnl, entryFillPx, exitPx, type));
            openSide = 0;
            entryFillPx = 0;
            return;
        }
        sessionNetPts += pnl;
        sessionTrades++;
        if (pnl > 0) {
            sessionWins++;
            consecutiveLosses = 0;
        } else {
            consecutiveLosses++;
        }
        Log.info(String.format(
                "CFADE TRADE CLOSED %s entry=%.2f exit=%.2f via=%s pnl=%.2f pts | session n=%d wins=%d net=%.2f consecLoss=%d",
                openSide > 0 ? "LONG" : "SHORT", entryFillPx, exitPx, type, pnl,
                sessionTrades, sessionWins, sessionNetPts, consecutiveLosses));
        openSide = 0;
        entryFillPx = 0;
        double kill = killMaxSessionLossPts == null ? 0.0 : killMaxSessionLossPts;
        if (kill > 0 && sessionNetPts <= -kill && !tradingHalted) {
            tradingHalted = true;
            Log.info(String.format(
                    "CFADE KILL SWITCH — session net %.2f ≤ −%.0f pts. No further orders today (signals still plotted).",
                    sessionNetPts, kill));
        }
    }

    @Override
    public void onOrderExecuted(velox.api.layer1.data.ExecutionInfo executionInfo) {
        // Replay SIM: ExecutionInfo.price is often populated when averageFillPrice is not.
        // Entry refinement only — exits already resolved from limit/stop in onOrderUpdated.
        if (executionInfo == null || openSide == 0) {
            return;
        }
        double px = executionInfo.price;
        if (Double.isNaN(px) || px <= 0) {
            return;
        }
        double pip = pips > 0 ? pips : 0.25;
        double chart = orderPriceToChart(px, pip);
        if (!(chart > 0) || Double.isNaN(chart)) {
            return;
        }
        // If we still only have the signal estimate, upgrade from the execution print.
        if (!(entryFillPx > 0) || Double.isNaN(entryFillPx)) {
            entryFillPx = chart;
            Log.info(String.format("CFADE entry from execution px=%.2f", entryFillPx));
        }
    }

    private void maybeRollSessionDate(long tsNs) {
        LocalDate d = ukDate(tsNs);
        if (sessionDateUk == null) {
            sessionDateUk = d;
            return;
        }
        if (!d.equals(sessionDateUk)) {
            Log.info(String.format("CFADE new UK session %s (prev %s): n=%d wins=%d net=%.2f halted=%s",
                    d, sessionDateUk, sessionTrades, sessionWins, sessionNetPts, tradingHalted));
            sessionDateUk = d;
            resetSessionStats("new_day");
        }
    }

    private void resetSessionStats(String via) {
        sessionNetPts = 0;
        sessionTrades = 0;
        sessionWins = 0;
        consecutiveLosses = 0;
        tradingHalted = false;
        if (!"init".equals(via)) {
            Log.info("CFADE session stats reset (" + via + ")");
        }
    }

    // ------------------------------------------------------------ account plumbing (as MAT2)

    private void installAccountListener(Api api) {
        try {
            provider = api.getProvider();
            if (provider == null) {
                Log.info("CFADE account listener: provider null — dropdown account not tracked");
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
            Log.info("CFADE account listener install failed: " + t);
        }
    }

    private void refreshTradingAccountId(String reason) {
        String resolved = resolveAccountId();
        boolean unchanged = (resolved == null && tradingAccountId == null)
                || (resolved != null && resolved.equals(tradingAccountId));
        if (unchanged && !"init".equals(reason) && !"selection".equals(reason)) {
            return;
        }
        tradingAccountId = resolved;
        String detail = "null(primary/unspecified)";
        if (resolved != null) {
            AccountInfo info = accountListManager.getAccountById(resolved);
            detail = info != null
                    ? resolved + " name=" + info.shortName + " sim=" + info.isSimulated()
                    : resolved;
        }
        Log.info("CFADE account [" + reason + "] " + detail);
    }

    private String resolveAccountId() {
        if (!Boolean.TRUE.equals(useSelectedAccount) || primaryAlias == null) {
            return null;
        }
        Optional<String> selected = accountListManager.getSelectedAccountIdForAlias(primaryAlias);
        if (selected.isPresent()) {
            String id = selected.get();
            return (id == null || id.isEmpty()) ? null : id;
        }
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

    // ------------------------------------------------------------ time helpers

    private static ZonedDateTime ukTime(long tsNs) {
        long sec = tsNs / 1_000_000_000L;
        long nano = tsNs % 1_000_000_000L;
        return Instant.ofEpochSecond(sec, nano).atZone(UK);
    }

    private static int ukMinuteOfDay(long tsNs) {
        ZonedDateTime z = ukTime(tsNs);
        return z.getHour() * 60 + z.getMinute();
    }

    private static LocalDate ukDate(long tsNs) {
        return ukTime(tsNs).toLocalDate();
    }

    private static String formatTime(long tsNs) {
        if (tsNs <= 0) {
            return "unknown";
        }
        try {
            return TIME_FMT.format(ukTime(tsNs));
        } catch (Exception e) {
            return "ns=" + tsNs;
        }
    }
}
