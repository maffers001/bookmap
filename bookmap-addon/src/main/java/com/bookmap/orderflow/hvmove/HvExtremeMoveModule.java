package com.bookmap.orderflow.hvmove;

import java.awt.Color;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import com.bookmap.orderflow.core.HvExtremeMoveTracker;
import com.bookmap.orderflow.core.HvExtremeMoveTracker.Side;
import com.bookmap.orderflow.core.HvExtremeMoveTracker.Signal;
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
import velox.api.layer1.simplified.HistoricalDataListener;
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
 * HV Extreme Move AutoTrade: HV tip → clean Move pts leg → signal at tip;
 * optional market entry with fixed TP/SL brackets.
 *
 * <p>LONG after HV trough + clean move up; SHORT after HV peak + clean move down.
 * Default: signals only ({@code Enable trading} off).
 */
@Layer1SimpleAttachable
@Layer1TradingStrategy
@Layer1MultiAccountTradingSupported
@Layer1StrategyName("HV Extreme Move AutoTrade")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class HvExtremeMoveModule implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener,
        HistoricalModeListener,
        PositionListener,
        OrdersListener {

    // --- Signal (tuned defaults from live panel) ---
    @Parameter(name = "Move pts", step = 0.25)
    Double movePts = 20.0;

    @Parameter(name = "Confirm pts", step = 0.25)
    Double confirmPts = 10.0;

    @Parameter(name = "Peak band pts", step = 0.25)
    Double peakBandPts = 5.0;

    @Parameter(name = "High vol mult", step = 0.1)
    Double highVolMult = 2.0;

    @Parameter(name = "Min path efficiency (0=off)", step = 0.05)
    Double minPathEfficiency = 0.0;

    @Parameter(name = "Max pullback pts (0=off)", step = 0.25)
    Double maxPullbackPts = 5.0;

    @Parameter(name = "Max leg sec (0=off)", step = 1.0)
    Double maxLegSec = 0.0;

    @Parameter(name = "Profile lookback min", step = 1.0)
    Double lookbackMin = 15.0;

    @Parameter(name = "Cooldown sec", step = 5.0)
    Double cooldownSec = 30.0;

    // --- Display ---
    @Parameter(name = "Show longs", step = 1.0)
    Boolean showLongs = true;

    @Parameter(name = "Show shorts", step = 1.0)
    Boolean showShorts = true;

    @Parameter(name = "Show signal", step = 1.0)
    Boolean showSignal = true;

    @Parameter(name = "Show armed", step = 1.0)
    Boolean showArmed = false;

    @Parameter(name = "Show vol ratio", step = 1.0)
    Boolean showVolRatio = false;

    @Parameter(name = "Show path ER", step = 1.0)
    Boolean showPathEr = true;

    @Parameter(name = "Show in-play lines", step = 1.0)
    Boolean showInPlay = true;

    // --- Trading ---
    @Parameter(name = "Enable trading (OFF for signal-only)")
    Boolean enableTrading = false;

    @Parameter(name = "Use selected account (dropdown)")
    Boolean useSelectedAccount = true;

    @Parameter(name = "Order size", step = 1.0)
    Double orderSize = 1.0;

    @Parameter(name = "Take profit pts", step = 0.25)
    Double takeProfitPts = 10.0;

    @Parameter(name = "Stop loss pts", step = 0.25)
    Double stopLossPts = 10.0;

    @Parameter(name = "Use brackets")
    Boolean useBrackets = true;

    @Parameter(name = "Flat-only (no pyramid)")
    Boolean flatOnly = true;

    private double pips = 0.25;
    private long nowNs;
    private String primaryAlias;
    private Api api;
    private HvExtremeMoveTracker tracker;

    private Indicator signalIndicator;
    private Indicator armedIndicator;
    private Indicator volRatioIndicator;
    private Indicator pathErIndicator;
    private Indicator peakLine;
    private Indicator troughLine;
    private Indicator targetLine;
    private Indicator fireLong;
    private Indicator fireShort;

    private int lastSignalSide;
    private int signalHold;
    private double lastFireLong = Double.NaN;
    private double lastFireShort = Double.NaN;
    private Side lastArmedLog = Side.NONE;

    private volatile boolean realtimeStarted;
    private int position;
    private int netFills;
    private boolean flatLocked;
    private int openSide;
    private double entryFillPx;
    private String tradingAccountId;
    private final Map<String, Integer> filledByOrderId = new HashMap<>();
    private final AccountListManager accountListManager = new AccountListManager();
    private Layer1ApiProvider provider;
    private Layer1ApiAdminAdapter adminListener;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.api = api;
        this.primaryAlias = alias;
        pips = info.pips > 0 ? info.pips : 0.25;
        realtimeStarted = false;
        position = 0;
        netFills = 0;
        flatLocked = false;
        openSide = 0;
        entryFillPx = 0;
        filledByOrderId.clear();
        tradingAccountId = null;
        tracker = buildTracker();

        signalIndicator = api.registerIndicator("HV Ext Signal", GraphType.BOTTOM);
        armedIndicator = api.registerIndicator("HV Ext Armed", GraphType.BOTTOM);
        volRatioIndicator = api.registerIndicator("HV Ext Vol ratio", GraphType.BOTTOM);
        pathErIndicator = api.registerIndicator("HV Ext Path ER", GraphType.BOTTOM);

        peakLine = api.registerIndicator("HV Ext Peak", GraphType.PRIMARY);
        troughLine = api.registerIndicator("HV Ext Trough", GraphType.PRIMARY);
        targetLine = api.registerIndicator("HV Ext Target", GraphType.PRIMARY);
        fireLong = api.registerIndicator("HV Ext Fire Long", GraphType.PRIMARY);
        fireShort = api.registerIndicator("HV Ext Fire Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 210, 60));
        armedIndicator.setColor(new Color(255, 150, 40));
        volRatioIndicator.setColor(new Color(120, 180, 255));
        pathErIndicator.setColor(new Color(180, 140, 255));

        peakLine.setColor(new Color(220, 80, 80));
        troughLine.setColor(new Color(40, 170, 100));
        targetLine.setColor(new Color(200, 200, 80));
        fireLong.setColor(new Color(0, 230, 110));
        fireShort.setColor(new Color(255, 40, 40));
        peakLine.setWidth(2);
        troughLine.setWidth(2);
        targetLine.setWidth(1);
        fireLong.setWidth(3);
        fireShort.setWidth(3);

        AxisGroup signalAxis = new AxisGroup();
        signalAxis.add(signalIndicator);
        signalAxis.add(armedIndicator);

        installAccountListener(api);
        refreshTradingAccountId("init");

        Log.info(String.format(
                "HVEXT AT init alias=%s pips=%.4f move=%.1f confirm=%.1f band=%.1f highMult=%.1f "
                        + "minEff=%.2f maxPull=%.1f maxLeg=%.0f trading=%s tp=%.1f sl=%.1f size=%.0f",
                alias,
                pips,
                movePts,
                confirmPts,
                peakBandPts,
                highVolMult,
                minPathEfficiency,
                maxPullbackPts,
                maxLegSec,
                Boolean.TRUE.equals(enableTrading),
                takeProfitPts,
                stopLossPts,
                orderSize));
    }

    private HvExtremeMoveTracker buildTracker() {
        int move = Math.max(1, ptsToTicks(movePts == null ? 20.0 : movePts));
        int confirm = Math.max(1, ptsToTicks(confirmPts == null ? 10.0 : confirmPts));
        int band = Math.max(0, ptsToTicks(peakBandPts == null ? 5.0 : peakBandPts));
        double mult = highVolMult == null ? 2.0 : highVolMult;
        long lookbackNs = (long) ((lookbackMin == null ? 15.0 : lookbackMin) * 60_000_000_000L);
        long coolNs = (long) ((cooldownSec == null ? 30.0 : cooldownSec) * 1_000_000_000L);
        double minEff = minPathEfficiency == null ? 0.0 : Math.max(0.0, minPathEfficiency);
        int maxPull = maxPullbackPts == null || maxPullbackPts <= 0
                ? 0
                : Math.max(0, ptsToTicks(maxPullbackPts));
        long maxLegNs = maxLegSec == null || maxLegSec <= 0
                ? 0L
                : (long) (maxLegSec * 1_000_000_000L);
        return new HvExtremeMoveTracker(
                move, confirm, band, mult, lookbackNs, coolNs, minEff, maxPull, maxLegNs);
    }

    private int ptsToTicks(double pts) {
        if (pips <= 0) {
            return (int) Math.round(pts);
        }
        return (int) Math.round(pts / pips);
    }

    @Override
    public void onTimestamp(long t) {
        nowNs = t;
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        refreshTradingAccountId("realtime");
        Log.info("HVEXT AT realtime started — orders enabled when trading=ON");
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        if (tracker == null || size <= 0) {
            return;
        }
        int tick = PriceTicks.apiPriceToTick(price, pips);
        long t = nowNs > 0 ? nowNs : System.nanoTime();
        tracker.onTrade(t, tick, size);

        Signal sig = tracker.pollSignal();
        if (sig != null) {
            onFire(sig);
        }
    }

    private void onFire(Signal sig) {
        boolean wantLong = sig.side == Side.LONG && Boolean.TRUE.equals(showLongs);
        boolean wantShort = sig.side == Side.SHORT && Boolean.TRUE.equals(showShorts);
        if (!wantLong && !wantShort) {
            return;
        }

        double tipChart = PriceTicks.toChartPoints(sig.extremeTick, pips);
        double entryChart = PriceTicks.toChartPoints(sig.triggerTick, pips);
        lastSignalSide = sig.side == Side.LONG ? 1 : -1;
        signalHold = 20;
        if (sig.side == Side.LONG) {
            lastFireLong = sig.extremeTick;
            lastFireShort = Double.NaN;
        } else {
            lastFireShort = sig.extremeTick;
            lastFireLong = Double.NaN;
        }

        boolean wantTrade = Boolean.TRUE.equals(enableTrading);
        boolean willTrade = wantTrade && realtimeStarted && api != null;

        Log.info(String.format(
                "HV EXT FIRE %s extreme=%d tipChart=%.2f entry~%.2f trigger=%d move=%d "
                        + "vol=%.0f ratio=%.2f eff=%.2f pullbackTicks=%d legSec=%.2f "
                        + "trading=%s realtime=%s pos=%d",
                sig.side,
                sig.extremeTick,
                tipChart,
                entryChart,
                sig.triggerTick,
                sig.moveTicks,
                sig.extremeVol,
                sig.volRatio,
                sig.pathEfficiency,
                sig.maxPullbackTicks,
                sig.legSec,
                wantTrade,
                realtimeStarted,
                flatCheckPosition()));

        if (willTrade) {
            sendBracketOrder(sig.side == Side.LONG, entryChart);
        } else if (wantTrade && !realtimeStarted) {
            Log.info("HVEXT AT ORDER skipped (catchup/history — waiting for realtime)");
        }
    }

    private int flatCheckPosition() {
        return position != 0 ? position : netFills;
    }

    private void sendBracketOrder(boolean isBuy, double entryPricePoints) {
        int size = Math.max(1, orderSize == null ? 1 : orderSize.intValue());
        double tpPts = takeProfitPts == null ? 10.0 : takeProfitPts;
        double slPts = stopLossPts == null ? 10.0 : stopLossPts;
        double pip = pips > 0 ? pips : 0.25;
        boolean brackets = Boolean.TRUE.equals(useBrackets);
        int tpTicks = brackets ? Math.max(1, (int) Math.round(tpPts / pip)) : 0;
        int slTicks = brackets ? Math.max(1, (int) Math.round(slPts / pip)) : 0;

        if (Boolean.TRUE.equals(flatOnly) && (flatCheckPosition() != 0 || flatLocked)) {
            Log.info("HVEXT AT ORDER blocked flat-only pos=" + position
                    + " netFills=" + netFills + " locked=" + flatLocked);
            return;
        }
        flatLocked = true;
        refreshTradingAccountId("order");
        try {
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
            openSide = isBuy ? 1 : -1;
            entryFillPx = entryPricePoints;
            Log.info(String.format(
                    "HVEXT AT ORDER sent %s size=%d entry~%.2f tp=%d ticks (~%.2f pts) "
                            + "sl=%d ticks (~%.2f pts) account=%s",
                    isBuy ? "BUY" : "SELL",
                    size,
                    entryPricePoints,
                    tpTicks,
                    tpPts,
                    slTicks,
                    slPts,
                    tradingAccountId == null ? "null(primary)" : tradingAccountId));
        } catch (Throwable t) {
            flatLocked = false;
            openSide = 0;
            Log.info("HVEXT AT ORDER failed: " + t);
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
            openSide = 0;
            entryFillPx = 0;
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

        if (openSide == 0) {
            return;
        }
        boolean entryDir = (order.isBuy && openSide > 0) || (!order.isBuy && openSide < 0);
        if (entryDir && (order.type == OrderType.MKT || order.fakeMarketOrder)) {
            double fillPx = resolveFillPx(order);
            if (fillPx > 0 && !Double.isNaN(fillPx)) {
                entryFillPx = fillPx;
            }
            Log.info(String.format("HVEXT AT trade open %s entry=%.2f",
                    openSide > 0 ? "LONG" : "SHORT", entryFillPx));
            return;
        }
        boolean exitDir = (order.isBuy && openSide < 0) || (!order.isBuy && openSide > 0);
        if (exitDir) {
            double exitPx = resolveFillPx(order);
            double pnl = openSide * (exitPx - entryFillPx);
            Log.info(String.format(
                    "HVEXT AT TRADE CLOSED %s entry=%.2f exit=%.2f via=%s pnl=%.2f pts",
                    openSide > 0 ? "LONG" : "SHORT",
                    entryFillPx,
                    exitPx,
                    order.type,
                    pnl));
            openSide = 0;
            entryFillPx = 0;
        }
    }

    @Override
    public void onOrderExecuted(velox.api.layer1.data.ExecutionInfo executionInfo) {
        // no-op; fills handled in onOrderUpdated
    }

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
        return entryFillPx;
    }

    /** Order prices are usually already chart points; tape tick indices need ×pips. */
    private double orderPriceToChart(double orderPx, double pip) {
        if (orderPx >= 80_000.0) {
            return orderPx * pip;
        }
        return orderPx;
    }

    private void installAccountListener(Api api) {
        try {
            provider = api.getProvider();
            if (provider == null) {
                Log.info("HVEXT AT account listener: provider null");
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
            Log.info("HVEXT AT account listener install failed: " + t);
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
        Log.info("HVEXT AT account [" + reason + "] "
                + (resolved == null ? "null(primary)" : resolved));
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

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        if (signalIndicator == null || tracker == null) {
            return;
        }

        if (Boolean.TRUE.equals(showSignal)) {
            signalIndicator.addPoint(lastSignalSide);
        } else {
            signalIndicator.addPoint(Double.NaN);
        }
        if (signalHold > 0) {
            signalHold--;
            if (signalHold == 0) {
                lastSignalSide = 0;
            }
        }

        Side watching = tracker.watchingSide();
        boolean drawLong = Boolean.TRUE.equals(showLongs);
        boolean drawShort = Boolean.TRUE.equals(showShorts);
        boolean drawInPlay = Boolean.TRUE.equals(showInPlay);

        double armed = 0;
        double peak = Double.NaN;
        double trough = Double.NaN;
        double target = Double.NaN;

        if (watching == Side.SHORT && drawShort) {
            armed = -1;
            if (drawInPlay) {
                peak = tracker.extremeTick();
                target = tracker.extremeTick() - tracker.moveTicks();
            }
        } else if (watching == Side.LONG && drawLong) {
            armed = 1;
            if (drawInPlay) {
                trough = tracker.extremeTick();
                target = tracker.extremeTick() + tracker.moveTicks();
            }
        }

        if (watching != lastArmedLog) {
            if (watching != Side.NONE) {
                Log.info(String.format(
                        "HV EXT WATCH %s extreme=%d target=%d volRatio=%.2f",
                        watching,
                        tracker.extremeTick(),
                        watching == Side.LONG
                                ? tracker.extremeTick() + tracker.moveTicks()
                                : tracker.extremeTick() - tracker.moveTicks(),
                        tracker.currentExtremeVolRatio()));
            } else if (lastArmedLog != Side.NONE) {
                Log.info("HV EXT CLEAR was=" + lastArmedLog);
            }
            lastArmedLog = watching;
        }

        if (Boolean.TRUE.equals(showArmed)) {
            armedIndicator.addPoint(armed);
        } else {
            armedIndicator.addPoint(Double.NaN);
        }
        if (Boolean.TRUE.equals(showVolRatio)) {
            volRatioIndicator.addPoint(tracker.currentExtremeVolRatio());
        } else {
            volRatioIndicator.addPoint(Double.NaN);
        }
        if (Boolean.TRUE.equals(showPathEr)) {
            pathErIndicator.addPoint(tracker.currentPathEfficiency());
        } else {
            pathErIndicator.addPoint(Double.NaN);
        }

        peakLine.addPoint(peak);
        troughLine.addPoint(trough);
        targetLine.addPoint(target);
        fireLong.addPoint(lastFireLong);
        fireShort.addPoint(lastFireShort);
    }

    @Override
    public void stop() {
        if (provider != null && adminListener != null) {
            try {
                provider.removeListener(adminListener);
            } catch (Throwable ignored) {
                // ignore
            }
        }
    }
}
