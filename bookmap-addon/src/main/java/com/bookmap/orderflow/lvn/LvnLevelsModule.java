package com.bookmap.orderflow.lvn;

import java.awt.Color;
import java.awt.Font;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.LvnLiveVap;
import com.bookmap.orderflow.core.LvnStackFile;
import com.bookmap.orderflow.core.MarketProfileTracker;
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
import velox.api.layer1.simplified.HistoricalModeListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Display-only <b>Levels</b>: LVN / HVN / DOP from a tick VAP seed plus developing
 * session Market Profile (VPOC / VAH / VAL) from live Bookmap prints.
 *
 * <p>Knobs live on {@link LvnLevelsPanel} / {@link LvnLevelsState}. No orders.
 *
 * <p>Node detection is deferred until {@link #onRealtimeStart()} so historical
 * "Loading events" is not blocked by VAP rebuilds. Profile accumulates from DOP
 * open (same UK clock as the DOP line).
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Levels")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class LvnLevelsModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        TimeListener,
        IntervalListener,
        HistoricalModeListener {

    private static final ZoneId UK = ZoneId.of("Europe/London");
    /** Cyan LVNs (matches prior confluence look). */
    private static final Color COL_LVN = new Color(0, 255, 255);
    /** Lime green HVNs. */
    private static final Color COL_HVN = new Color(50, 255, 50);
    /** Dark blue DOP (session open). */
    private static final Color COL_DOP = new Color(20, 60, 180);
    /** Gold VPOC. */
    private static final Color COL_VPOC = new Color(255, 210, 40);
    /** Magenta VAH / VAL. */
    private static final Color COL_VA = new Color(255, 110, 200);

    private double pips = 0.25;
    private String instrumentRole = "OTHER";
    private String alias = "";
    private long nowNs;
    private int bestBidTick = Integer.MIN_VALUE;
    private int bestAskTick = Integer.MIN_VALUE;
    private double lastMidPts = Double.NaN;

    private final Indicator[] lvnAbove = new Indicator[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private final Indicator[] lvnBelow = new Indicator[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private final Indicator[] hvnAbove = new Indicator[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private final Indicator[] hvnBelow = new Indicator[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    /** Price-stable slot state — avoids verticals when nearest-N ranking reshuffles. */
    private final LevelSlot[] lvnAboveSlot = new LevelSlot[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private final LevelSlot[] lvnBelowSlot = new LevelSlot[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private final LevelSlot[] hvnAboveSlot = new LevelSlot[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private final LevelSlot[] hvnBelowSlot = new LevelSlot[LvnStackFile.MAX_DISPLAY_EACH_SIDE];
    private Indicator dopMarker;
    private final LevelSlot dopSlot = new LevelSlot();
    private Indicator vpocMarker;
    private Indicator vahMarker;
    private Indicator valMarker;
    private final LevelSlot vpocSlot = new LevelSlot();
    private final LevelSlot vahSlot = new LevelSlot();
    private final LevelSlot valSlot = new LevelSlot();

    /** Developing session volume profile (VPOC / VA) from DOP open. */
    private MarketProfileTracker profileTracker;
    private boolean profileSessionOpen;

    /**
     * One Bookmap indicator lane bound to a fixed chart price until that price
     * leaves the display set. On a price change for the same lane, emit NaN for
     * one interval before the new value so Bookmap does not draw a vertical.
     */
    private static final class LevelSlot {
        double boundPts = Double.NaN;
        double plottedPts = Double.NaN;
        double iconPts = Double.NaN;
        boolean breaking;
    }

    private long lastRebuildFeedNs;
    private long lastRebuildWallNs;
    private boolean dopCaptured;
    private LocalDate lastUkDate;
    /** Cached UK day bounds (epoch ns) so onTimestamp stays allocation-free. */
    private long cachedDayStartNs = Long.MIN_VALUE;
    private long cachedNextDayNs = Long.MIN_VALUE;
    /** Epoch ns of today's DOP open (recomputed on day change / Apply). */
    private long dopOpenNs = Long.MAX_VALUE;
    private boolean thresholdsDirty = true;
    /** False during Bookmap catchup / Loading events (incl. replay scrub). */
    private volatile boolean realtimeStarted;
    private volatile boolean pendingRebuild;
    private final Runnable applyHook = this::requestRebuild;

    private static final java.util.concurrent.ConcurrentHashMap<String, BufferedImage> ICON_CACHE =
            new java.util.concurrent.ConcurrentHashMap<>();

    /** Wall-clock minimum between node detections (feed time can jump hours during catchup). */
    private static final long MIN_REBUILD_WALL_NS = 3_000_000_000L;
    /** Treat feed time moving backward by more than this as a scrub → re-enter catchup. */
    private static final long REWIND_DETECT_NS = 500_000_000L;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.alias = alias;
        this.realtimeStarted = false;
        this.pendingRebuild = false;
        this.lastRebuildFeedNs = 0;
        this.lastRebuildWallNs = 0;
        pips = info.pips > 0 ? info.pips : 0.25;
        instrumentRole = InstrumentClassifier.classify(info);

        for (int i = 0; i < LvnStackFile.MAX_DISPLAY_EACH_SIDE; i++) {
            lvnAbove[i] = api.registerIndicator(
                    "LVN Above " + (i + 1), GraphType.PRIMARY, Double.NaN, true, true);
            lvnBelow[i] = api.registerIndicator(
                    "LVN Below " + (i + 1), GraphType.PRIMARY, Double.NaN, true, true);
            hvnAbove[i] = api.registerIndicator(
                    "HVN Above " + (i + 1), GraphType.PRIMARY, Double.NaN, true, true);
            hvnBelow[i] = api.registerIndicator(
                    "HVN Below " + (i + 1), GraphType.PRIMARY, Double.NaN, true, true);
            styleLevelLine(lvnAbove[i], COL_LVN, 50);
            styleLevelLine(lvnBelow[i], COL_LVN, 50);
            styleLevelLine(hvnAbove[i], COL_HVN, 40);
            styleLevelLine(hvnBelow[i], COL_HVN, 40);
            lvnAboveSlot[i] = new LevelSlot();
            lvnBelowSlot[i] = new LevelSlot();
            hvnAboveSlot[i] = new LevelSlot();
            hvnBelowSlot[i] = new LevelSlot();
        }
        dopMarker = api.registerIndicator("DOP", GraphType.PRIMARY, Double.NaN, true, true);
        styleLevelLine(dopMarker, COL_DOP, 55, 6); // 2× LVN/HVN width
        dopSlot.boundPts = dopSlot.plottedPts = dopSlot.iconPts = Double.NaN;
        dopSlot.breaking = false;

        vpocMarker = api.registerIndicator("VPOC", GraphType.PRIMARY, Double.NaN, true, true);
        vahMarker = api.registerIndicator("VAH", GraphType.PRIMARY, Double.NaN, true, true);
        valMarker = api.registerIndicator("VAL", GraphType.PRIMARY, Double.NaN, true, true);
        styleLevelLine(vpocMarker, COL_VPOC, 52);
        styleLevelLine(vahMarker, COL_VA, 48);
        styleLevelLine(valMarker, COL_VA, 48);
        // Do not addPoint here — Bookmap rejects indicator points before feed time exists.
        resetSlotState(vpocSlot);
        resetSlotState(vahSlot);
        resetSlotState(valSlot);

        profileTracker = new MarketProfileTracker(pips, LvnLevelsState.valueAreaFrac());
        profileSessionOpen = false;

        if (isEs() || isNq()) {
            LvnLevelsState.registerAttach(instrumentRole);
            LvnLevelsState.registerApplyHook(applyHook);
        }
        // Panel + seed I/O off this thread — Bookmap attach must stay snappy.
        LvnLevelsPanel.showPanel();

        Log.info(String.format(Locale.US,
                "Levels init alias=%s role=%s pips=%.4f (LVN nodes after realtime; MP from DOP open)",
                alias, instrumentRole, pips));
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        Log.info("Levels realtime start role=" + instrumentRole
                + " pendingRebuild=" + pendingRebuild);
        // Detect on the next interval tick — never during Loading events.
        pendingRebuild = true;
        thresholdsDirty = true;
    }

    /**
     * Replay scrub re-feeds history without a dedicated callback — detect rewind
     * and drop back into catchup so we do not run node detection on the load thread.
     */
    private void enterCatchupIfRewind(long t) {
        if (nowNs <= 0 || t >= nowNs - REWIND_DETECT_NS) {
            return;
        }
        if (realtimeStarted) {
            Log.info(String.format(Locale.US,
                    "Levels catchup (rewind) role=%s from=%d to=%d",
                    instrumentRole, nowNs, t));
        }
        realtimeStarted = false;
        pendingRebuild = true;
        dopCaptured = false;
        clearSlot(dopSlot, dopMarker);
        resetProfile();
        LvnLiveVap vap = LvnLevelsState.vapFor(instrumentRole);
        if (vap != null) {
            vap.clearLive();
        }
        LvnLevelsState.clearDop(instrumentRole);
    }

    @Override
    public void onTimestamp(long t) {
        enterCatchupIfRewind(t);
        nowNs = t;
        // Fast path: still inside the cached UK day — no conversion, no lock.
        if (t >= cachedDayStartNs && t < cachedNextDayNs) {
            return;
        }
        ZonedDateTime z = ukTime(t);
        LocalDate day = z.toLocalDate();
        cachedDayStartNs = day.atStartOfDay(UK).toInstant().toEpochMilli() * 1_000_000L;
        cachedNextDayNs = day.plusDays(1).atStartOfDay(UK).toInstant().toEpochMilli() * 1_000_000L;
        if (lastUkDate == null || !day.equals(lastUkDate)) {
            lastUkDate = day;
            dopCaptured = false;
            clearSlot(dopSlot, dopMarker);
            LvnLevelsState.clearDop(instrumentRole);
            resetProfile();
            pendingRebuild = true;
            thresholdsDirty = true;
        }
        LvnLevelsState.setSessionDate(day);
        maybeOpenProfile();
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
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
        } else if (bestAskTick == Integer.MIN_VALUE || tick < bestAskTick) {
            bestAskTick = tick;
        }
        int mid = midTick();
        if (mid != Integer.MIN_VALUE) {
            lastMidPts = mid * pips;
        }
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        if (!(isEs() || isNq()) || size <= 0 || nowNs <= 0) {
            return;
        }
        int tick = resolveTradeTick(price);
        double chartPts = tick * pips;
        lastMidPts = chartPts;

        // Capture DOP on the first print at/after open even during catchup so the
        // price is the true open — but plotting stays gated until open (see plotLevels).
        // Never run node detection here.
        if (!dopCaptured) {
            maybeCaptureDop(chartPts);
        }
        maybeOpenProfile();
        if (profileTracker != null && profileSessionOpen) {
            profileTracker.onTrade(tick, size);
        }
        LvnLiveVap vap = LvnLevelsState.vapFor(instrumentRole);
        if (vap != null && vap.hasSeed()) {
            vap.onTradeNs(tick, size, nowNs);
        }
        // Alert on trade path too — 250ms interval alone can miss a fast touch.
        if (realtimeStarted && nowNs > 0) {
            LvnLevelsState.onMidUpdate(instrumentRole, lastMidPts, nowNs);
        }
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_250_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        if (!(isEs() || isNq()) || !realtimeStarted) {
            // Skip all work during Loading events / scrub catchup.
            return;
        }
        maybeRebuild(pendingRebuild);
        plotLevels();
        if (Double.isFinite(lastMidPts) && lastMidPts > 0 && nowNs > 0) {
            LvnLevelsState.onMidUpdate(instrumentRole, lastMidPts, nowNs);
        }
    }

    /** Apply / seed-bind hook — queue only; the interval tick does the work off the EDT. */
    private void requestRebuild() {
        pendingRebuild = true;
        thresholdsDirty = true;
        if (!realtimeStarted) {
            Log.info("Levels rebuild queued until realtime role=" + instrumentRole);
        }
    }

    private void maybeRebuild(boolean force) {
        if (!realtimeStarted) {
            pendingRebuild = true;
            return;
        }
        LvnLiveVap vap = LvnLevelsState.vapFor(instrumentRole);
        if (vap == null || !vap.hasSeed()) {
            return;
        }
        long wall = System.nanoTime();
        // Hard wall-clock gate: catchup can advance feed time by hours between intervals.
        if (!force && lastRebuildWallNs > 0 && wall - lastRebuildWallNs < MIN_REBUILD_WALL_NS) {
            return;
        }
        LvnLevelsState.MarketKnobs knobs = LvnLevelsState.knobsFor(instrumentRole);
        long intervalNs = Math.max(1, knobs.refreshMin) * 60_000_000_000L;
        if (!force && !pendingRebuild && lastRebuildFeedNs > 0 && nowNs > 0
                && nowNs - lastRebuildFeedNs < intervalNs) {
            return;
        }
        LocalDateTime asOf = nowNs > 0 ? ukTime(nowNs).toLocalDateTime() : LocalDateTime.now(UK);
        double mid = Double.isFinite(lastMidPts) ? lastMidPts : 0;
        LvnLevelsState.MarketLevels lv = LvnLevelsState.rebuild(instrumentRole, mid, asOf);
        lastRebuildFeedNs = nowNs > 0 ? nowNs : wall;
        lastRebuildWallNs = wall;
        pendingRebuild = false;
        if (lv != null) {
            Log.info(String.format(Locale.US,
                    "Levels rebuilt %s mid=%.2f LVN above=%s below=%s HVN above=%s below=%s allLvn=%d",
                    instrumentRole, lv.mid,
                    fmtPrices(lv.lvnAbove), fmtPrices(lv.lvnBelow),
                    fmtPrices(lv.hvnAbove), fmtPrices(lv.hvnBelow),
                    lv.allLvns.size()));
        }
    }

    private void maybeCaptureDop(double chartPts) {
        if (dopCaptured || !Double.isFinite(chartPts)) {
            return;
        }
        if (thresholdsDirty) {
            refreshThresholds();
        }
        if (nowNs >= dopOpenNs) {
            dopCaptured = true;
            LvnLevelsState.setDop(instrumentRole, chartPts);
            maybeOpenProfile();
            Log.info(String.format(Locale.US, "Levels DOP %s=%.2f feedNs=%d",
                    instrumentRole, chartPts, nowNs));
        }
    }

    private void resetProfile() {
        profileSessionOpen = false;
        if (profileTracker != null) {
            profileTracker.reset();
        }
        // Hide only once feed time is live; otherwise just clear slot state.
        if (nowNs > 0) {
            clearSlot(vpocSlot, vpocMarker);
            clearSlot(vahSlot, vahMarker);
            clearSlot(valSlot, valMarker);
        } else {
            resetSlotState(vpocSlot);
            resetSlotState(vahSlot);
            resetSlotState(valSlot);
        }
    }

    /** Start developing MP at/after DOP open (same UK clock as DOP). */
    private void maybeOpenProfile() {
        if (profileTracker == null || profileSessionOpen || nowNs <= 0) {
            return;
        }
        if (thresholdsDirty) {
            refreshThresholds();
        }
        if (dopOpenNs == Long.MAX_VALUE || nowNs < dopOpenNs) {
            return;
        }
        profileTracker.setVaFrac(LvnLevelsState.valueAreaFrac());
        profileTracker.markSessionOpen(nowNs);
        profileSessionOpen = true;
        Log.info(String.format(Locale.US,
                "Levels MP session open role=%s vaPct=%.0f",
                instrumentRole, LvnLevelsState.valueAreaPct()));
    }

    private void refreshThresholds() {
        thresholdsDirty = false;
        if (lastUkDate == null) {
            dopOpenNs = Long.MAX_VALUE;
            return;
        }
        LvnLevelsState.MarketKnobs knobs = LvnLevelsState.knobsFor(instrumentRole);
        LocalTime open = LvnLiveVap.parseUkHm(knobs.dopOpenUkHm);
        dopOpenNs = LocalDateTime.of(lastUkDate, open).atZone(UK).toInstant().toEpochMilli() * 1_000_000L;
    }

    private void plotLevels() {
        LvnLevelsState.MarketLevels lv = LvnLevelsState.levelsFor(instrumentRole);
        LvnLevelsState.MarketKnobs knobs = LvnLevelsState.knobsFor(instrumentRole);
        boolean showLvn = LvnLevelsState.showLvn();
        boolean showHvn = LvnLevelsState.showHvn();
        boolean showDop = LvnLevelsState.showDop();

        plotStableGroup(lvnAbove, lvnAboveSlot, showLvn && lv != null ? lv.lvnAbove : null,
                knobs.lvnAbove, COL_LVN);
        plotStableGroup(lvnBelow, lvnBelowSlot, showLvn && lv != null ? lv.lvnBelow : null,
                knobs.lvnBelow, COL_LVN);
        plotStableGroup(hvnAbove, hvnAboveSlot, showHvn && lv != null ? lv.hvnAbove : null,
                knobs.hvnAbove, COL_HVN);
        plotStableGroup(hvnBelow, hvnBelowSlot, showHvn && lv != null ? lv.hvnBelow : null,
                knobs.hvnBelow, COL_HVN);

        // Do not draw the DOP line until today's open has printed.
        if (thresholdsDirty) {
            refreshThresholds();
        }
        boolean pastOpen = dopCaptured
                && lastUkDate != null
                && dopOpenNs != Long.MAX_VALUE
                && nowNs >= dopOpenNs;
        double dop = LvnLevelsState.dopFor(instrumentRole);
        if (showDop && pastOpen && Double.isFinite(dop) && dop > 0) {
            dopSlot.boundPts = dop;
            plotSlot(dopMarker, dopSlot, COL_DOP, true);
        } else {
            clearSlot(dopSlot, dopMarker);
        }

        plotProfileLevels();
    }

    private void plotProfileLevels() {
        if (profileTracker == null) {
            return;
        }
        profileTracker.setVaFrac(LvnLevelsState.valueAreaFrac());
        MarketProfileTracker.Levels mp = profileTracker.levels();
        boolean ready = LvnLevelsState.marketProfileEnabled()
                && profileSessionOpen
                && mp.ready();
        plotMpSlot(vpocMarker, vpocSlot, ready && LvnLevelsState.showVpoc(), mp.vpocPts, COL_VPOC);
        plotMpSlot(vahMarker, vahSlot, ready && LvnLevelsState.showVah(), mp.vahPts, COL_VA);
        plotMpSlot(valMarker, valSlot, ready && LvnLevelsState.showVal(), mp.valPts, COL_VA);
    }

    /**
     * Developing MP levels must stay continuous: every interval emits the current
     * price with no NaN “break” (that LVN trick leaves gaps when VAH/VAL/VPOC moves).
     * Plain line only — no price icons.
     */
    private void plotMpSlot(Indicator ind, LevelSlot slot, boolean show, double pts, Color color) {
        if (ind == null) {
            return;
        }
        if (!show || !Double.isFinite(pts) || !(pts > 0) || nowNs <= 0) {
            if (nowNs > 0) {
                ind.addPoint(Double.NaN);
            }
            resetSlotState(slot);
            return;
        }
        double axis = toPrimaryAxis(pts);
        ind.addPoint(axis);
        slot.boundPts = pts;
        slot.plottedPts = pts;
        slot.iconPts = Double.NaN;
        slot.breaking = false;
    }

    /**
     * Bind each displayed price to a stable indicator lane so ranking reshuffles
     * do not drag one series from price A to B (vertical connector).
     */
    private void plotStableGroup(
            Indicator[] inds,
            LevelSlot[] slots,
            List<Double> prices,
            int maxShow,
            Color color) {
        int n = inds.length;
        Set<Long> desiredKeys = new HashSet<>(8);
        if (prices != null && maxShow > 0) {
            int lim = Math.min(maxShow, prices.size());
            for (int i = 0; i < lim; i++) {
                Double px = prices.get(i);
                if (px != null && px > 0 && Double.isFinite(px)) {
                    desiredKeys.add(priceKey(px));
                }
            }
        }

        // Release lanes whose price left the set.
        for (int i = 0; i < n; i++) {
            LevelSlot s = slots[i];
            if (Double.isFinite(s.boundPts) && !desiredKeys.contains(priceKey(s.boundPts))) {
                clearSlot(s, inds[i]);
            }
        }

        // Bind new prices to free lanes.
        if (prices != null && maxShow > 0) {
            int lim = Math.min(maxShow, prices.size());
            for (int i = 0; i < lim; i++) {
                Double px = prices.get(i);
                if (px == null || !(px > 0) || !Double.isFinite(px)) {
                    continue;
                }
                if (findSlotForPrice(slots, px) >= 0) {
                    continue;
                }
                int free = findFreeSlot(slots);
                if (free < 0) {
                    break;
                }
                LevelSlot s = slots[free];
                s.boundPts = px;
                s.plottedPts = Double.NaN;
                s.iconPts = Double.NaN;
                s.breaking = false;
            }
        }

        for (int i = 0; i < n; i++) {
            LevelSlot s = slots[i];
            if (Double.isFinite(s.boundPts)) {
                plotSlot(inds[i], s, color, true);
            } else {
                hideLine(inds[i]);
            }
        }
    }

    private static long priceKey(double pts) {
        return Math.round(pts * 4.0); // 0.25 grid
    }

    private static int findSlotForPrice(LevelSlot[] slots, double pts) {
        long key = priceKey(pts);
        for (int i = 0; i < slots.length; i++) {
            if (Double.isFinite(slots[i].boundPts) && priceKey(slots[i].boundPts) == key) {
                return i;
            }
        }
        return -1;
    }

    private static int findFreeSlot(LevelSlot[] slots) {
        for (int i = 0; i < slots.length; i++) {
            if (!Double.isFinite(slots[i].boundPts)) {
                return i;
            }
        }
        return -1;
    }

    private static void resetSlotState(LevelSlot s) {
        if (s == null) {
            return;
        }
        s.boundPts = Double.NaN;
        s.plottedPts = Double.NaN;
        s.iconPts = Double.NaN;
        s.breaking = false;
    }

    private static void clearSlot(LevelSlot s, Indicator ind) {
        hideLine(ind);
        resetSlotState(s);
    }

    /**
     * Emit one sample for a bound slot. If the bound price changed on this lane,
     * spend one interval on NaN before drawing the new level.
     *
     * @param withIcon if true, draw a price tag (LVN/HVN/DOP); MP lines stay plain
     */
    private void plotSlot(Indicator ind, LevelSlot s, Color color, boolean withIcon) {
        if (ind == null || s == null || !Double.isFinite(s.boundPts) || !(s.boundPts > 0)) {
            hideLine(ind);
            return;
        }
        double chartPts = s.boundPts;
        if (s.breaking) {
            // Previous interval already emitted NaN — start the new horizontal.
            s.breaking = false;
            double axis = toPrimaryAxis(chartPts);
            ind.addPoint(axis);
            s.plottedPts = chartPts;
            if (withIcon) {
                BufferedImage icon = renderPriceIcon(formatPriceLabel(chartPts), color);
                ind.addIcon(axis, icon, 4, icon.getHeight() / 2);
                s.iconPts = chartPts;
            }
            return;
        }
        if (Double.isFinite(s.plottedPts) && Math.abs(s.plottedPts - chartPts) > 1e-6) {
            // Same lane, new price — break for one interval (no vertical connector).
            ind.addPoint(Double.NaN);
            s.plottedPts = Double.NaN;
            s.iconPts = Double.NaN;
            s.breaking = true;
            return;
        }
        double axis = toPrimaryAxis(chartPts);
        ind.addPoint(axis);
        s.plottedPts = chartPts;
        if (withIcon && (Double.isNaN(s.iconPts) || Math.abs(s.iconPts - chartPts) > 1e-6)) {
            BufferedImage icon = renderPriceIcon(formatPriceLabel(chartPts), color);
            ind.addIcon(axis, icon, 4, icon.getHeight() / 2);
            s.iconPts = chartPts;
        }
    }

    @Override
    public void stop() {
        if (isEs() || isNq()) {
            LvnLevelsState.unregisterApplyHook(applyHook);
            LvnLevelsState.unregisterAttach(instrumentRole);
        }
        Log.info("Levels stop alias=" + alias + " role=" + instrumentRole);
    }

    private boolean isEs() {
        return "ES".equals(instrumentRole);
    }

    private boolean isNq() {
        return "NQ".equals(instrumentRole);
    }

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
            if (bestDist < 2_000) {
                return best;
            }
        }
        if ("ES".equals(instrumentRole)) {
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

    private double toPrimaryAxis(double chartPts) {
        if (Double.isNaN(chartPts)) {
            return Double.NaN;
        }
        return pips > 0 ? chartPts / pips : chartPts;
    }

    private static void styleLevelLine(Indicator ind, Color color, int priority) {
        styleLevelLine(ind, color, priority, 3);
    }

    private static void styleLevelLine(Indicator ind, Color color, int priority, int width) {
        if (ind == null) {
            return;
        }
        ind.setColor(color);
        ind.setWidth(width);
        ind.setRenderPriority(priority);
    }

    private static void hideLine(Indicator ind) {
        if (ind != null) {
            ind.addPoint(Double.NaN);
        }
    }

    private static String formatPriceLabel(double chartPts) {
        if (Math.abs(chartPts - Math.rint(chartPts)) < 1e-9) {
            return String.format(Locale.US, "%.0f", chartPts);
        }
        return String.format(Locale.US, "%.2f", chartPts);
    }

    private static String fmtPrices(java.util.List<Double> prices) {
        if (prices == null || prices.isEmpty()) {
            return "[]";
        }
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < prices.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(String.format(Locale.US, "%.2f", prices.get(i)));
        }
        return sb.append(']').toString();
    }

    private static BufferedImage renderPriceIcon(String text, Color color) {
        String key = color.getRGB() + "|" + text;
        BufferedImage cached = ICON_CACHE.get(key);
        if (cached != null) {
            return cached;
        }
        Font font = new Font(Font.SANS_SERIF, Font.BOLD, 12);
        BufferedImage tmp = new BufferedImage(1, 1, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g0 = tmp.createGraphics();
        g0.setFont(font);
        int w = g0.getFontMetrics().stringWidth(text) + 8;
        int h = g0.getFontMetrics().getHeight() + 4;
        g0.dispose();
        BufferedImage img = new BufferedImage(w, h, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g = img.createGraphics();
        g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
        g.setFont(font);
        g.setColor(new Color(0, 0, 0, 160));
        g.fillRoundRect(0, 0, w, h, 6, 6);
        g.setColor(color);
        g.drawString(text, 4, h - 6);
        g.dispose();
        if (ICON_CACHE.size() < 512) {
            ICON_CACHE.putIfAbsent(key, img);
        }
        return img;
    }

    private static ZonedDateTime ukTime(long tsNs) {
        return Instant.ofEpochSecond(tsNs / 1_000_000_000L, tsNs % 1_000_000_000L).atZone(UK);
    }
}
