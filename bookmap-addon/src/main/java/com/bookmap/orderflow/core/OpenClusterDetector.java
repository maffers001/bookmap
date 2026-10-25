package com.bookmap.orderflow.core;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

/**
 * ES open-cluster ∩ LVN confluence detector (Bookmap-free).
 *
 * <p>Ports research {@code export_open_clusters} ES config + outer-wall
 * confluence from {@code study_es_lvn_bracket_8_24}:
 * <ul>
 *   <li>Snapshot resting book at UK 14:30</li>
 *   <li>Bright prices ≥ p99 (floor 20), merge within 6 ticks, top 10/side</li>
 *   <li>Near LVN2/LVN3 ≤ nearLvnPts; outer wall; outside offset entry</li>
 *   <li>Skip if |level − open mid| &lt; minOpenDistPts</li>
 * </ul>
 *
 * <p>v1 ranks by raw size at snapshot. Optional first-seen map approximates
 * age weight when depth has been observed long enough before the snap.
 */
public final class OpenClusterDetector {

    public enum Side { BID, ASK }

    public enum LevelState {
        WATCH,
        ARMED,
        FILLED,
        DONE,
        PULLED
    }

    public static final class Config {
        public double pips = 0.25;
        public double bookRadiusPts = 60.0;
        public int clusterTicks = 6;
        public Double maxWidthTicks = null;
        public double nearMidExcludePts = 1.0;
        public int largeSizeMin = 20;
        public double brightSizePct = 99.0;
        public int topNPerSide = 10;
        public double nearLvnPts = 10.0;
        public double offsetPts = 2.0;
        public double minOpenDistPts = 5.0;
        /** Age weight scale (s); full weight by 15 min when first-seen known. */
        public double ageScaleS = 900.0;
        public double ageWMin = 0.25;
        public double ageWMax = 1.0;
        public double ageCapS = 7200.0;
        public boolean useAgeWeight = true;
    }

    public static final class Cluster {
        public final Side side;
        public final double centrePts;
        public final double pxLo;
        public final double pxHi;
        public final int size;
        public final double distMidPts;
        public final double score;
        public final int nPrices;
        public final int pxLoTick;
        public final int pxHiTick;

        Cluster(Side side, double centrePts, double pxLo, double pxHi, int size,
                double distMidPts, double score, int nPrices, int pxLoTick, int pxHiTick) {
            this.side = side;
            this.centrePts = centrePts;
            this.pxLo = pxLo;
            this.pxHi = pxHi;
            this.size = size;
            this.distMidPts = distMidPts;
            this.score = score;
            this.nPrices = nPrices;
            this.pxLoTick = pxLoTick;
            this.pxHiTick = pxHiTick;
        }
    }

    public static final class ArmedLevel {
        public final Side side;
        public final double centrePts;
        public final double lvnPx;
        public final double lvnDist;
        public final String lvnId;
        public final double levelPx;
        public final String levelSrc;
        public final double entryPts;
        public final int size;
        public final double distOpen;
        public final double score;
        public LevelState state;
        public long armNs;
        public long fillNs;
        public String orderId;

        ArmedLevel(Side side, double centrePts, double lvnPx, double lvnDist, String lvnId,
                   double levelPx, String levelSrc, double entryPts, int size,
                   double distOpen, double score, long armNs) {
            this.side = side;
            this.centrePts = centrePts;
            this.lvnPx = lvnPx;
            this.lvnDist = lvnDist;
            this.lvnId = lvnId;
            this.levelPx = levelPx;
            this.levelSrc = levelSrc;
            this.entryPts = entryPts;
            this.size = size;
            this.distOpen = distOpen;
            this.score = score;
            this.state = LevelState.ARMED;
            this.armNs = armNs;
            this.fillNs = 0;
        }

        /** +1 long (BID fade) / −1 short (ASK fade). */
        public int tradeSide() {
            return side == Side.BID ? 1 : -1;
        }

        /** Still eligible for Next Long/Short and resting entries (liquidity pull is display-only). */
        public boolean isTradeable() {
            return state == LevelState.ARMED || state == LevelState.PULLED;
        }
    }

    public static final class DisplayRow {
        public final Side side;
        public final double centrePts;
        public final double levelPts;
        public final int size;
        public final double distMidPts;
        public final double lvnDist;
        public final double lvnPx;
        public final boolean confluence;
        public final LevelState state;

        public DisplayRow(Side side, double centrePts, double levelPts, int size,
                          double distMidPts, double lvnDist, double lvnPx,
                          boolean confluence, LevelState state) {
            this.side = side;
            this.centrePts = centrePts;
            this.levelPts = levelPts;
            this.size = size;
            this.distMidPts = distMidPts;
            this.lvnDist = lvnDist;
            this.lvnPx = lvnPx;
            this.confluence = confluence;
            this.state = state;
        }
    }

    private final Config cfg;
    private final TreeMap<Integer, Integer> bids = new TreeMap<>(Comparator.reverseOrder());
    private final TreeMap<Integer, Integer> asks = new TreeMap<>();
    /** First-seen ns per (sideBit<<63 | priceTick) for age approx. */
    private final Map<Long, Long> firstSeenNs = new HashMap<>();

    private Integer lastTradeTick;
    private long nowNs;
    private boolean snapped;
    private long snapNs;
    private double openMidPts;
    private double lvn2;
    private double lvn3;
    private String rankingMode = "size";
    private final List<ArmedLevel> armed = new ArrayList<>();
    private final List<Cluster> snapClusters = new ArrayList<>();

    public OpenClusterDetector(Config cfg) {
        this.cfg = cfg == null ? new Config() : cfg;
    }

    public Config config() {
        return cfg;
    }

    public boolean isSnapped() {
        return snapped;
    }

    public long snapNs() {
        return snapNs;
    }

    public double openMidPts() {
        return openMidPts;
    }

    public String rankingMode() {
        return rankingMode;
    }

    public List<ArmedLevel> armedLevels() {
        return armed;
    }

    public List<Cluster> snapClusters() {
        return snapClusters;
    }

    public void setLvns(double lvn2, double lvn3) {
        this.lvn2 = lvn2;
        this.lvn3 = lvn3;
    }

    public double lvn2() {
        return lvn2;
    }

    public double lvn3() {
        return lvn3;
    }

    public void resetDay() {
        snapped = false;
        snapNs = 0;
        openMidPts = 0;
        armed.clear();
        snapClusters.clear();
        rankingMode = "size";
    }

    public void onTimestamp(long ns) {
        this.nowNs = ns;
    }

    public void onDepth(boolean isBid, int priceTick, int size) {
        TreeMap<Integer, Integer> book = isBid ? bids : asks;
        long key = bookKey(isBid, priceTick);
        if (size <= 0) {
            book.remove(priceTick);
        } else {
            book.put(priceTick, size);
            firstSeenNs.putIfAbsent(key, nowNs > 0 ? nowNs : System.nanoTime());
        }
    }

    public void onTrade(int priceTick, long tsNs) {
        lastTradeTick = priceTick;
        if (tsNs > 0) {
            nowNs = tsNs;
        }
    }

    public int bidLevels() {
        return bids.size();
    }

    public int askLevels() {
        return asks.size();
    }

    /** Best bid tick — {@code bids} is reverse-ordered, so {@code firstKey()} is highest. */
    public Integer bestBidTick() {
        return bids.isEmpty() ? null : bids.firstKey();
    }

    /** Best ask tick — {@code asks} is natural-ordered, so {@code firstKey()} is lowest. */
    public Integer bestAskTick() {
        return asks.isEmpty() ? null : asks.firstKey();
    }

    /** BBO mid: (best bid + best ask) / 2. Do NOT use bids.lastKey() — that's the worst bid. */
    public Integer midTick() {
        Integer bb = bestBidTick();
        Integer ba = bestAskTick();
        if (bb != null && ba != null) {
            return (bb + ba) / 2;
        }
        return lastTradeTick;
    }

    public Double midPts() {
        Integer m = midTick();
        return m == null ? null : m * cfg.pips;
    }

    /**
     * Snapshot at {@code snapNs}. Returns number of armed confluence levels.
     * Idempotent: second call is a no-op unless {@link #resetDay()} was called.
     *
     * <p>Research {@code export_open_clusters} uses last-trade mid at snap; we use
     * it only when it agrees with BBO (tape/depth tick units can disagree), else BBO.
     * Crossed prices are filtered without mutating the live book.
     */
    public int snapshot(long snapNs, double openMidOverride) {
        if (snapped) {
            return armed.size();
        }
        this.snapNs = snapNs;
        this.nowNs = snapNs;
        Integer bboMid = midTick();
        Integer tradeMid = lastTradeTick;
        Integer midTick;
        String midSrc;
        if (tradeMid != null && bboMid != null && Math.abs(tradeMid - bboMid) <= 80) {
            midTick = tradeMid;
            midSrc = "trade";
        } else if (bboMid != null) {
            midTick = bboMid;
            midSrc = "bbo";
        } else if (tradeMid != null) {
            midTick = tradeMid;
            midSrc = "trade-only";
        } else {
            return 0;
        }
        if (openMidOverride > 0 && cfg.pips > 0) {
            int fromOverride = (int) Math.round(openMidOverride / cfg.pips);
            if (Math.abs(fromOverride - midTick) <= 8) {
                midTick = fromOverride;
                midSrc = midSrc + "+override";
            }
        }
        openMidPts = midTick * cfg.pips;
        int brightFloor = computeBrightFloor(midTick);
        List<Cluster> all = clusterBook(midTick, brightFloor);
        int rawBid = 0;
        int rawAsk = 0;
        for (Cluster c : all) {
            if (c.side == Side.BID) {
                rawBid++;
            } else {
                rawAsk++;
            }
        }
        lastSnapDiag = String.format(Locale.US,
                "midTick=%d src=%s bbo=%s trade=%s bids=%d asks=%d brightFloor=%d "
                        + "rawClusters=%d (BID=%d ASK=%d) lvn2=%.2f lvn3=%.2f",
                midTick, midSrc,
                bboMid == null ? "—" : bboMid.toString(),
                lastTradeTick == null ? "—" : lastTradeTick.toString(),
                bids.size(), asks.size(), brightFloor, all.size(), rawBid, rawAsk,
                lvn2, lvn3);
        // Rank: age-weighted if we have first-seen coverage, else raw size
        rankingMode = decideRankingMode(all, snapNs);
        all.sort((a, b) -> Double.compare(b.score, a.score));
        List<Cluster> kept = new ArrayList<>();
        int nBid = 0;
        int nAsk = 0;
        for (Cluster c : all) {
            if (c.side == Side.BID) {
                if (nBid >= cfg.topNPerSide) {
                    continue;
                }
                nBid++;
            } else {
                if (nAsk >= cfg.topNPerSide) {
                    continue;
                }
                nAsk++;
            }
            kept.add(c);
        }
        snapClusters.clear();
        snapClusters.addAll(kept);
        armed.clear();
        StringBuilder rejects = new StringBuilder();
        for (Cluster c : kept) {
            NearestLvn lv = nearestLvn(c.centrePts);
            if (lv == null || lv.dist > cfg.nearLvnPts) {
                if (rejects.length() < 400) {
                    rejects.append(String.format(Locale.US,
                            " %s@%.2f nearLvn=%.2f;",
                            c.side, c.centrePts, lv == null ? -1 : lv.dist));
                }
                continue;
            }
            Anchor a = outerAnchor(c.side, c.centrePts, lv.px);
            double distOpen = Math.abs(a.levelPx - openMidPts);
            if (distOpen < cfg.minOpenDistPts) {
                if (rejects.length() < 400) {
                    rejects.append(String.format(Locale.US,
                            " %s@%.2f minOpen=%.2f;", c.side, c.centrePts, distOpen));
                }
                continue;
            }
            double entry = c.side == Side.BID
                    ? a.levelPx - cfg.offsetPts
                    : a.levelPx + cfg.offsetPts;
            ArmedLevel neu = new ArmedLevel(
                    c.side, c.centrePts, lv.px, lv.dist, lv.id,
                    a.levelPx, a.src, entry, c.size, distOpen, c.score, snapNs);
            // Outer-wall collapse: several clusters near the same LVN share one entry —
            // keep a single arm per (side, entry) so we don't double-order.
            addArmedUniqueEntry(neu);
        }
        if (rejects.length() > 0) {
            lastSnapDiag += " reject:" + rejects;
        }
        snapped = true;
        return armed.size();
    }

    /**
     * Nearest tradeable level on {@code side} to {@code refMidPts} (usually open mid).
     * {@link LevelState#PULLED} stays eligible — pull is display-only, not a disarm.
     *
     * @param currentMidPts when non-null, skip marketable entries (BID at/above or
     *                      ASK at/below current mid) so we don't place instant fills;
     *                      pass null for chart highlights so Next Short stays visible
     *                      after price runs through.
     */
    public ArmedLevel nearestArmed(Side side, double refMidPts, Double currentMidPts) {
        ArmedLevel best = null;
        double bestDist = Double.POSITIVE_INFINITY;
        for (ArmedLevel a : armed) {
            if (a.side != side || !a.isTradeable()) {
                continue;
            }
            if (currentMidPts != null) {
                if (side == Side.BID && a.entryPts >= currentMidPts - 1e-9) {
                    continue;
                }
                if (side == Side.ASK && a.entryPts <= currentMidPts + 1e-9) {
                    continue;
                }
            }
            double d = Math.abs(a.entryPts - refMidPts);
            if (d < bestDist) {
                bestDist = d;
                best = a;
            }
        }
        return best;
    }

    /** Nearest tradeable on side to ref mid (no marketability filter — for display). */
    public ArmedLevel nearestArmed(Side side, double refMidPts) {
        return nearestArmed(side, refMidPts, null);
    }

    /** Count tradeable arms on each book side (for diagnostics). */
    public int countTradeable(Side side) {
        int n = 0;
        for (ArmedLevel a : armed) {
            if (a.side == side && a.isTradeable()) {
                n++;
            }
        }
        return n;
    }

    /** Keep higher-score (else larger) arm when entry prices match within half a tick. */
    private void addArmedUniqueEntry(ArmedLevel neu) {
        double tol = Math.max(1e-6, cfg.pips * 0.5);
        for (int i = 0; i < armed.size(); i++) {
            ArmedLevel ex = armed.get(i);
            if (ex.side != neu.side) {
                continue;
            }
            if (Math.abs(ex.entryPts - neu.entryPts) > tol) {
                continue;
            }
            boolean better = neu.score > ex.score
                    || (neu.score == ex.score && neu.size > ex.size)
                    || (neu.score == ex.score && neu.size == ex.size && neu.lvnDist < ex.lvnDist);
            if (better) {
                armed.set(i, neu);
            }
            return;
        }
        armed.add(neu);
    }

    /** Last {@link #snapshot} diagnostics (bright floor / mid sources); empty before first snap. */
    public String lastSnapDiag() {
        return lastSnapDiag == null ? "" : lastSnapDiag;
    }

    private String lastSnapDiag = "";

    /** Live watch list for console (pre- or post-snap). Caps at topN per side. */
    public List<DisplayRow> displayRows(int maxPerSide) {
        List<DisplayRow> out = new ArrayList<>();
        if (snapped && !armed.isEmpty()) {
            for (ArmedLevel a : armed) {
                out.add(new DisplayRow(
                        a.side, a.centrePts, a.levelPx, a.size,
                        Math.abs(a.centrePts - openMidPts), a.lvnDist, a.lvnPx,
                        true, a.state));
            }
            out.sort((x, y) -> Double.compare(x.distMidPts, y.distMidPts));
            return out;
        }
        Double mid = midPts();
        if (mid == null) {
            return out;
        }
        int midTick = (int) Math.round(mid / cfg.pips);
        int brightFloor = computeBrightFloor(midTick);
        List<Cluster> all = clusterBook(midTick, brightFloor);
        all.sort((a, b) -> Double.compare(b.score, a.score));
        int nBid = 0;
        int nAsk = 0;
        for (Cluster c : all) {
            if (c.side == Side.BID) {
                if (nBid >= maxPerSide) {
                    continue;
                }
                nBid++;
            } else {
                if (nAsk >= maxPerSide) {
                    continue;
                }
                nAsk++;
            }
            NearestLvn lv = nearestLvn(c.centrePts);
            boolean conf = lv != null && lv.dist <= cfg.nearLvnPts;
            double level = c.centrePts;
            if (conf) {
                level = outerAnchor(c.side, c.centrePts, lv.px).levelPx;
            }
            out.add(new DisplayRow(
                    c.side, c.centrePts, level, c.size, c.distMidPts,
                    lv == null ? Double.NaN : lv.dist,
                    lv == null ? Double.NaN : lv.px,
                    conf, LevelState.WATCH));
        }
        return out;
    }

    public void markFilled(ArmedLevel level, long fillNs) {
        if (level == null) {
            return;
        }
        level.state = LevelState.FILLED;
        level.fillNs = fillNs;
    }

    public void markDone(ArmedLevel level) {
        if (level != null) {
            level.state = LevelState.DONE;
        }
    }

    /** Update PULLED display if band size collapsed (does not disarm by default). */
    public void refreshPullDisplay() {
        if (!snapped) {
            return;
        }
        for (ArmedLevel a : armed) {
            if (a.state != LevelState.ARMED) {
                continue;
            }
            TreeMap<Integer, Integer> book = a.side == Side.BID ? bids : asks;
            int lo = (int) Math.round(a.centrePts / cfg.pips) - cfg.clusterTicks;
            int hi = (int) Math.round(a.centrePts / cfg.pips) + cfg.clusterTicks;
            // Prefer original band if we stored ticks — approximate via centre±width
            int sum = 0;
            for (Map.Entry<Integer, Integer> e : book.entrySet()) {
                if (e.getKey() >= lo && e.getKey() <= hi) {
                    sum += e.getValue();
                }
            }
            if (sum < Math.max(1, a.size / 4)) {
                a.state = LevelState.PULLED;
            }
        }
    }

    // ------------------------------------------------------------------ clustering

    private int computeBrightFloor(int midTick) {
        int rad = (int) Math.round(cfg.bookRadiusPts / cfg.pips);
        List<Integer> sizes = new ArrayList<>();
        for (Map.Entry<Integer, Integer> e : bids.entrySet()) {
            if (Math.abs(e.getKey() - midTick) <= rad && e.getValue() > 0) {
                sizes.add(e.getValue());
            }
        }
        for (Map.Entry<Integer, Integer> e : asks.entrySet()) {
            if (Math.abs(e.getKey() - midTick) <= rad && e.getValue() > 0) {
                sizes.add(e.getValue());
            }
        }
        int pctFloor = cfg.largeSizeMin;
        if (!sizes.isEmpty()) {
            sizes.sort(Integer::compareTo);
            int idx = Math.min(sizes.size() - 1,
                    Math.max(0, (int) Math.ceil(cfg.brightSizePct / 100.0 * sizes.size()) - 1));
            pctFloor = Math.max(cfg.largeSizeMin, sizes.get(idx));
        }
        return pctFloor;
    }

    private List<Cluster> clusterBook(int midTick, int minPriceSize) {
        int rad = (int) Math.round(cfg.bookRadiusPts / cfg.pips);
        int lo = midTick - rad;
        int hi = midTick + rad;
        List<Cluster> out = new ArrayList<>();
        // Filter crossed vs mid without mutating the live book (snapshot used to
        // delete crossed prices permanently and could empty a side).
        out.addAll(clusterSide(Side.BID, filterBook(bids, lo, hi, minPriceSize, midTick, true), midTick));
        out.addAll(clusterSide(Side.ASK, filterBook(asks, lo, hi, minPriceSize, midTick, false), midTick));
        return out;
    }

    private static Map<Integer, Integer> filterBook(
            TreeMap<Integer, Integer> book, int lo, int hi, int minSize,
            int midTick, boolean isBid) {
        Map<Integer, Integer> out = new TreeMap<>();
        for (Map.Entry<Integer, Integer> e : book.entrySet()) {
            int p = e.getKey();
            int s = e.getValue();
            if (s < minSize || p < lo || p > hi) {
                continue;
            }
            if (isBid && p >= midTick) {
                continue;
            }
            if (!isBid && p <= midTick) {
                continue;
            }
            out.put(p, s);
        }
        return out;
    }

    private List<Cluster> clusterSide(Side side, Map<Integer, Integer> book, int midTick) {
        List<Integer> prices = new ArrayList<>(book.keySet());
        prices.sort(Integer::compareTo);
        List<Cluster> out = new ArrayList<>();
        if (prices.isEmpty()) {
            return out;
        }
        List<Integer> curPx = new ArrayList<>();
        List<Integer> curSz = new ArrayList<>();
        curPx.add(prices.get(0));
        curSz.add(book.get(prices.get(0)));
        for (int i = 1; i < prices.size(); i++) {
            int p = prices.get(i);
            boolean gapOk = p - curPx.get(curPx.size() - 1) <= cfg.clusterTicks;
            boolean widthOk = cfg.maxWidthTicks == null
                    || (p - curPx.get(0)) <= cfg.maxWidthTicks;
            if (gapOk && widthOk) {
                curPx.add(p);
                curSz.add(book.get(p));
            } else {
                Cluster c = packCluster(side, curPx, curSz, midTick);
                if (c != null) {
                    out.add(c);
                }
                curPx = new ArrayList<>();
                curSz = new ArrayList<>();
                curPx.add(p);
                curSz.add(book.get(p));
            }
        }
        Cluster last = packCluster(side, curPx, curSz, midTick);
        if (last != null) {
            out.add(last);
        }
        return out;
    }

    private Cluster packCluster(Side side, List<Integer> px, List<Integer> sz, int midTick) {
        double tot = 0;
        double wsum = 0;
        for (int i = 0; i < px.size(); i++) {
            tot += sz.get(i);
            wsum += px.get(i) * (double) sz.get(i);
        }
        if (tot <= 0) {
            return null;
        }
        int centreTick = (int) Math.round(wsum / tot);
        double centrePts = centreTick * cfg.pips;
        double dist = Math.abs(centrePts - midTick * cfg.pips);
        if (dist < cfg.nearMidExcludePts) {
            return null;
        }
        // support below mid / resistance above
        if (side == Side.BID && centrePts >= midTick * cfg.pips) {
            return null;
        }
        if (side == Side.ASK && centrePts <= midTick * cfg.pips) {
            return null;
        }
        int pxLo = px.get(0);
        int pxHi = px.get(px.size() - 1);
        double score = scoreCluster(side, px, sz, tot);
        return new Cluster(
                side, centrePts, pxLo * cfg.pips, pxHi * cfg.pips,
                (int) Math.round(tot), dist, score, px.size(), pxLo, pxHi);
    }

    private double scoreCluster(Side side, List<Integer> px, List<Integer> sz, double tot) {
        if (!cfg.useAgeWeight || snapNs <= 0 && nowNs <= 0) {
            return tot;
        }
        long asof = snapped ? snapNs : (nowNs > 0 ? nowNs : System.nanoTime());
        double wSum = 0;
        double raw = 0;
        boolean anyAge = false;
        for (int i = 0; i < px.size(); i++) {
            long key = bookKey(side == Side.BID, px.get(i));
            Long first = firstSeenNs.get(key);
            double s = sz.get(i);
            raw += s;
            if (first != null && first > 0 && asof > first) {
                anyAge = true;
                double ageS = Math.min(cfg.ageCapS, Math.max(0.0, (asof - first) / 1e9));
                double w = clip(ageS / cfg.ageScaleS, cfg.ageWMin, cfg.ageWMax);
                wSum += s * w;
            } else {
                // Unknown age → treat as age_cap (resting at data start)
                double w = clip(cfg.ageCapS / cfg.ageScaleS, cfg.ageWMin, cfg.ageWMax);
                wSum += s * w;
            }
        }
        if (!anyAge) {
            return tot;
        }
        double missing = Math.max(0.0, tot - raw);
        if (missing > 0) {
            wSum += missing * clip(cfg.ageCapS / cfg.ageScaleS, cfg.ageWMin, cfg.ageWMax);
        }
        return wSum;
    }

    private String decideRankingMode(List<Cluster> clusters, long snapNs) {
        if (!cfg.useAgeWeight) {
            return "size";
        }
        int known = 0;
        int total = 0;
        for (Cluster c : clusters) {
            for (int t = c.pxLoTick; t <= c.pxHiTick; t++) {
                // rough: only check endpoints
            }
            long k0 = bookKey(c.side == Side.BID, c.pxLoTick);
            long k1 = bookKey(c.side == Side.BID, c.pxHiTick);
            total += 2;
            if (firstSeenNs.containsKey(k0)) {
                known++;
            }
            if (firstSeenNs.containsKey(k1)) {
                known++;
            }
        }
        if (total > 0 && known * 2 >= total) {
            return "age_weighted";
        }
        return "size";
    }

    // ------------------------------------------------------------------ confluence

    private static final class NearestLvn {
        final String id;
        final double px;
        final double dist;

        NearestLvn(String id, double px, double dist) {
            this.id = id;
            this.px = px;
            this.dist = dist;
        }
    }

    private NearestLvn nearestLvn(double centre) {
        NearestLvn best = null;
        if (lvn2 != 0.0) {
            best = new NearestLvn("LVN2", lvn2, Math.abs(lvn2 - centre));
        }
        if (lvn3 != 0.0) {
            double d = Math.abs(lvn3 - centre);
            if (best == null || d < best.dist) {
                best = new NearestLvn("LVN3", lvn3, d);
            }
        }
        return best;
    }

    private static final class Anchor {
        final double levelPx;
        final String src;

        Anchor(double levelPx, String src) {
            this.levelPx = levelPx;
            this.src = src;
        }
    }

    /** Outer wall: BID/buy → min(cluster, LVN); ASK/sell → max(cluster, LVN). */
    static Anchor outerAnchor(Side side, double centre, double lvn) {
        if (side == Side.ASK) {
            return lvn >= centre ? new Anchor(lvn, "lvn") : new Anchor(centre, "cluster");
        }
        return lvn <= centre ? new Anchor(lvn, "lvn") : new Anchor(centre, "cluster");
    }

    private static double clip(double x, double lo, double hi) {
        return Math.max(lo, Math.min(hi, x));
    }

    private static long bookKey(boolean isBid, int priceTick) {
        return (isBid ? 0x8000_0000_0000_0000L : 0L) | (priceTick & 0x7fff_ffffL);
    }

    public String summary() {
        return String.format(Locale.US,
                "snapped=%s openMid=%.2f lvn2=%.2f lvn3=%.2f clusters=%d armed=%d rank=%s",
                snapped, openMidPts, lvn2, lvn3, snapClusters.size(), armed.size(), rankingMode);
    }
}
