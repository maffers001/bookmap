package com.bookmap.orderflow.profile;

import java.awt.Color;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.Locale;

import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.MarketProfileTracker;
import com.bookmap.orderflow.core.MarketProfileTracker.Levels;
import com.bookmap.orderflow.core.PriceTicks;

import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.common.Log;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyIndicator.GraphType;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Developing Market Profile from UK session open (default 14:30).
 *
 * <p>Accumulates trade volume by price from the open and plots live
 * <b>VAL</b>, <b>VAH</b>, <b>VPOC</b>, and <b>Daily Open</b> lines on the
 * primary chart. Value area expands contiguously from VPOC until the
 * configured volume fraction (default 70%) is covered.
 *
 * <p>ES tape often arrives as tick indices (~30k) below the generic 80k
 * heuristic — depth mid is used to disambiguate (same approach as LVN).
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Market Profile")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class MarketProfileModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        TimeListener,
        IntervalListener,
        HistoricalDataListener {

    private static final ZoneId UK = ZoneId.of("Europe/London");

    private static final Color COL_VPOC = new Color(255, 210, 40);
    private static final Color COL_VA = new Color(255, 110, 200); // VAH + VAL
    private static final Color COL_OPEN = new Color(30, 120, 255);

    @Parameter(name = "Session open UK min-of-day", step = 1.0)
    Double sessionOpenUkMin = 870.0; // 14:30

    @Parameter(name = "Value area %", step = 1.0)
    Double valueAreaPct = 70.0;

    @Parameter(name = "Show all lines", step = 1.0)
    Boolean showAllLines = true;

    @Parameter(name = "Show Daily Open", step = 1.0)
    Boolean showOpen = true;

    @Parameter(name = "Show VPOC", step = 1.0)
    Boolean showVpoc = true;

    @Parameter(name = "Show VAH", step = 1.0)
    Boolean showVah = true;

    @Parameter(name = "Show VAL", step = 1.0)
    Boolean showVal = true;

    @Parameter(name = "Verbose log", step = 1.0)
    Boolean verboseLog = false;

    private double pips = 0.25;
    private String instrumentRole = "OTHER";
    private long nowNs;
    private MarketProfileTracker tracker;

    private Indicator vpocMarker;
    private Indicator vahMarker;
    private Indicator valMarker;
    private Indicator openMarker;
    private Indicator totalVolIndicator;

    /** Best bid/ask tick from depth (for ES tape disambiguation). */
    private int bestBidTick = Integer.MIN_VALUE;
    private int bestAskTick = Integer.MIN_VALUE;

    /** First trade chart price at/after session open. */
    private double dailyOpenPts = Double.NaN;
    private boolean loggedFirstTrade;

    private LocalDate lastUkDate;
    private boolean openLogged;
    private long lastStatusLogNs;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        pips = info.pips > 0 ? info.pips : 0.25;
        instrumentRole = InstrumentClassifier.classify(info);
        tracker = new MarketProfileTracker(pips, vaFracFromParam());

        // All indicators MUST be registered here — Bookmap forbids registerIndicator later.
        openMarker = api.registerIndicator("MP Open", GraphType.PRIMARY, Double.NaN, true, true);
        vpocMarker = api.registerIndicator("MP VPOC", GraphType.PRIMARY, Double.NaN, true, true);
        vahMarker = api.registerIndicator("MP VAH", GraphType.PRIMARY, Double.NaN, true, true);
        valMarker = api.registerIndicator("MP VAL", GraphType.PRIMARY, Double.NaN, true, true);
        totalVolIndicator = api.registerIndicator("MP Total vol", GraphType.BOTTOM, 0.0, true, true);

        styleLevelLine(openMarker, COL_OPEN, 55);
        styleLevelLine(vpocMarker, COL_VPOC, 50);
        styleLevelLine(vahMarker, COL_VA, 45);
        styleLevelLine(valMarker, COL_VA, 45);
        totalVolIndicator.setColor(new Color(180, 180, 200));
        totalVolIndicator.setWidth(1);

        Log.info(String.format(Locale.US,
                "MP init alias=%s role=%s pips=%.4f openUkMin=%d vaPct=%.0f",
                alias, instrumentRole, pips, openUkMin(),
                valueAreaPct == null ? 70.0 : valueAreaPct));
    }

    @Override
    public void onTimestamp(long t) {
        nowNs = t;
        maybeRollDayAndOpen(t);
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        // Depth prices on this install are typically already tick indices (ES ~30k, NQ ~1e5).
        int tick = price;
        if (size <= 0) {
            if (isBid && tick == bestBidTick) {
                bestBidTick = Integer.MIN_VALUE;
            } else if (!isBid && tick == bestAskTick) {
                bestAskTick = Integer.MIN_VALUE;
            }
            return;
        }
        if (isBid) {
            if (bestBidTick == Integer.MIN_VALUE || tick > bestBidTick) {
                bestBidTick = tick;
            }
        } else {
            if (bestAskTick == Integer.MIN_VALUE || tick < bestAskTick) {
                bestAskTick = tick;
            }
        }
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        if (tracker == null || size <= 0) {
            return;
        }
        maybeRollDayAndOpen(nowNs > 0 ? nowNs : System.nanoTime());
        if (!tracker.isOpen()) {
            return;
        }
        int tick = resolveTradeTick(price);
        if (!loggedFirstTrade) {
            loggedFirstTrade = true;
            Log.info(String.format(Locale.US,
                    "MP first trade role=%s apiPx=%.4f tick=%d chart=%.2f mid=%s",
                    instrumentRole, price, tick, tick * pips, midTickStr()));
        }
        if (Double.isNaN(dailyOpenPts)) {
            dailyOpenPts = tick * pips;
            Log.info(String.format(Locale.US, "MP daily open=%.2f", dailyOpenPts));
        }
        tracker.onTrade(tick, size);
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        if (tracker == null || vpocMarker == null) {
            return;
        }
        tracker.setVaFrac(vaFracFromParam());
        Levels lv = tracker.levels();
        boolean master = Boolean.TRUE.equals(showAllLines);

        plotOrHide(openMarker, master && Boolean.TRUE.equals(showOpen), dailyOpenPts);
        if (lv.ready()) {
            plotOrHide(vpocMarker, master && Boolean.TRUE.equals(showVpoc), lv.vpocPts);
            plotOrHide(vahMarker, master && Boolean.TRUE.equals(showVah), lv.vahPts);
            plotOrHide(valMarker, master && Boolean.TRUE.equals(showVal), lv.valPts);
        } else {
            hideLine(vpocMarker);
            hideLine(vahMarker);
            hideLine(valMarker);
        }

        if (totalVolIndicator != null) {
            totalVolIndicator.addPoint(tracker.isOpen() ? tracker.totalVol() : 0.0);
        }

        maybeLogStatus(lv);
    }

    @Override
    public void stop() {
        // no-op
    }

    // ------------------------------------------------------------------ tick resolve (ES)

    /**
     * Map tape price → tick index. ES tick indices (~25k–35k) sit below the
     * generic 80k chart/tick split, so prefer depth mid when available; else
     * an ES-specific threshold (~15k separates chart pts from ticks).
     */
    private int resolveTradeTick(double price) {
        if (pips <= 0) {
            return (int) Math.round(price);
        }
        int asRaw = (int) Math.round(price);
        int asChart = (int) Math.round(price / pips);
        int mid = midTick();
        if (mid != Integer.MIN_VALUE) {
            int best = asRaw;
            int bestDist = Math.abs(asRaw - mid);
            int dChart = Math.abs(asChart - mid);
            if (dChart < bestDist) {
                best = asChart;
                bestDist = dChart;
            }
            // Only accept when reasonably near the book (avoid chasing a stale mid).
            if (bestDist < 2_000) {
                return best;
            }
        }
        if ("ES".equals(instrumentRole)) {
            // ES chart ~5–8k; ticks ~20–35k.
            return price >= 15_000.0 ? asRaw : asChart;
        }
        return PriceTicks.apiPriceToTick(price, pips);
    }

    private int midTick() {
        if (bestBidTick == Integer.MIN_VALUE || bestAskTick == Integer.MIN_VALUE) {
            return Integer.MIN_VALUE;
        }
        if (bestAskTick < bestBidTick) {
            return Integer.MIN_VALUE;
        }
        return (bestBidTick + bestAskTick) / 2;
    }

    private String midTickStr() {
        int m = midTick();
        return m == Integer.MIN_VALUE ? "—" : Integer.toString(m);
    }

    // ------------------------------------------------------------------ session

    private void maybeRollDayAndOpen(long tsNs) {
        if (tsNs <= 0) {
            return;
        }
        ZonedDateTime z = ukTime(tsNs);
        LocalDate day = z.toLocalDate();
        int minOfDay = z.getHour() * 60 + z.getMinute();
        int openMin = openUkMin();

        if (lastUkDate == null || !day.equals(lastUkDate)) {
            lastUkDate = day;
            openLogged = false;
            loggedFirstTrade = false;
            dailyOpenPts = Double.NaN;
            tracker.reset();
            tracker.setUkDayOfYear(z.getDayOfYear());
            Log.info("MP day reset — waiting for UK " + formatUkMin(openMin)
                    + " (" + day + ")");
        }

        if (!tracker.isOpen() && minOfDay >= openMin) {
            tracker.markSessionOpen(tsNs);
            dailyOpenPts = Double.NaN;
            loggedFirstTrade = false;
            if (!openLogged) {
                openLogged = true;
                Log.info(String.format(Locale.US,
                        "MP session open %s UK %02d:%02d role=%s vaPct=%.0f",
                        day,
                        openMin / 60,
                        openMin % 60,
                        instrumentRole,
                        valueAreaPct == null ? 70.0 : valueAreaPct));
            }
        }
    }

    private void maybeLogStatus(Levels lv) {
        if (!Boolean.TRUE.equals(verboseLog) || !tracker.isOpen() || !lv.ready()) {
            return;
        }
        if (nowNs - lastStatusLogNs < 30_000_000_000L) {
            return;
        }
        lastStatusLogNs = nowNs;
        Log.info(String.format(Locale.US,
                "MP status role=%s vol=%d open=%.2f vpoc=%.2f vah=%.2f val=%.2f covered=%.1f%%",
                instrumentRole, lv.totalVol,
                dailyOpenPts, lv.vpocPts, lv.vahPts, lv.valPts, 100.0 * lv.vaFracCovered));
    }

    // ------------------------------------------------------------------ plotting

    /**
     * Bookmap PRIMARY indicators on this install use tick indices as the Y
     * value (meter shows {@code value * pips}). Feeding chart points draws
     * lines off the visible heatmap.
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

    private static void hideLine(Indicator ind) {
        if (ind != null) {
            ind.addPoint(Double.NaN);
        }
    }

    private void plotOrHide(Indicator ind, boolean show, double chartPts) {
        if (ind == null) {
            return;
        }
        if (!show || !Double.isFinite(chartPts)) {
            ind.addPoint(Double.NaN);
            return;
        }
        ind.addPoint(toPrimaryAxis(chartPts));
    }

    // ------------------------------------------------------------------ params / time

    private int openUkMin() {
        int m = sessionOpenUkMin == null ? 870 : (int) Math.round(sessionOpenUkMin);
        if (m < 0) {
            return 0;
        }
        if (m > 24 * 60 - 1) {
            return 24 * 60 - 1;
        }
        return m;
    }

    private double vaFracFromParam() {
        double pct = valueAreaPct == null ? 70.0 : valueAreaPct;
        if (pct > 1.0) {
            return Math.min(1.0, Math.max(0.01, pct / 100.0));
        }
        return Math.min(1.0, Math.max(0.01, pct));
    }

    private static ZonedDateTime ukTime(long tsNs) {
        return Instant.ofEpochSecond(tsNs / 1_000_000_000L, tsNs % 1_000_000_000L).atZone(UK);
    }

    private static String formatUkMin(int minOfDay) {
        return String.format(Locale.US, "%02d:%02d", minOfDay / 60, minOfDay % 60);
    }
}
