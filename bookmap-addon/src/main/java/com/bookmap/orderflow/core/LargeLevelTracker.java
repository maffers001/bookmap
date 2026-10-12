package com.bookmap.orderflow.core;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Detects and tracks large resting liquidity near the BBO through approach,
 * contact, absorption/replenishment, withdrawal, and post-contact response.
 */
public final class LargeLevelTracker {

    public enum Side { BID, ASK }

    public enum EventLabel {
        NONE,
        ATTRACT,
        REPEL,
        ABSORB,
        WITHDRAW,
        DWELL_SUPPORT
    }

    public static final class TrackedLevel {
        public final Side side;
        public final int price;
        public final long firstSeenNs;
        public int initialSize;
        public int peakSize;
        public int currentSize;
        public long lastUpdateNs;
        public long nearPriceSinceNs = -1;
        public boolean contacted;
        public long contactNs = -1;
        public long executedInto;
        public int replenishCount;
        public int maxPenetrationTicks;
        public Integer midAtContact;
        public Double postMove250ms;
        public Double postMove1s;
        public Double postMove2s;
        public EventLabel label = EventLabel.NONE;
        public boolean closed;

        TrackedLevel(Side side, int price, int size, long nowNs) {
            this.side = side;
            this.price = price;
            this.initialSize = size;
            this.peakSize = size;
            this.currentSize = size;
            this.firstSeenNs = nowNs;
            this.lastUpdateNs = nowNs;
        }

        public double consumeRatio() {
            if (initialSize <= 0) {
                return executedInto > 0 ? Double.POSITIVE_INFINITY : 0.0;
            }
            return (double) executedInto / initialSize;
        }

        public double dwellSeconds(long nowNs) {
            if (nearPriceSinceNs < 0) {
                return 0.0;
            }
            return (nowNs - nearPriceSinceNs) / 1_000_000_000.0;
        }

        public double ageSeconds(long nowNs) {
            return (nowNs - firstSeenNs) / 1_000_000_000.0;
        }
    }

    private final TreeMap<Integer, Integer> bids = new TreeMap<>(Comparator.reverseOrder());
    private final TreeMap<Integer, Integer> asks = new TreeMap<>();
    private final Map<String, TrackedLevel> active = new LinkedHashMap<>();
    private final List<TrackedLevel> recentlyClosed = new ArrayList<>();

    private final RollingStats depthSizeStats;
    private int absoluteSizeThreshold;
    private double zScoreThreshold;
    private int tickRadius;
    private double dwellAlertSeconds;
    private int bestBid = Integer.MIN_VALUE;
    private int bestAsk = Integer.MAX_VALUE;
    private Integer lastTradePrice;
    private long nowNs;

    public LargeLevelTracker(int absoluteSizeThreshold, double zScoreThreshold,
                             int tickRadius, double dwellAlertSeconds, int statsWindow) {
        this.absoluteSizeThreshold = absoluteSizeThreshold;
        this.zScoreThreshold = zScoreThreshold;
        this.tickRadius = tickRadius;
        this.dwellAlertSeconds = dwellAlertSeconds;
        this.depthSizeStats = new RollingStats(statsWindow);
    }

    public void setAbsoluteSizeThreshold(int absoluteSizeThreshold) {
        this.absoluteSizeThreshold = absoluteSizeThreshold;
    }

    public void setZScoreThreshold(double zScoreThreshold) {
        this.zScoreThreshold = zScoreThreshold;
    }

    public void setTickRadius(int tickRadius) {
        this.tickRadius = tickRadius;
    }

    public void setDwellAlertSeconds(double dwellAlertSeconds) {
        this.dwellAlertSeconds = dwellAlertSeconds;
    }

    public void onTimestamp(long nanoseconds) {
        this.nowNs = nanoseconds;
        updatePostContactMoves();
    }

    public void onDepth(boolean isBid, int price, int size) {
        TreeMap<Integer, Integer> book = isBid ? bids : asks;
        if (size <= 0) {
            book.remove(price);
        } else {
            book.put(price, size);
            depthSizeStats.add(size);
        }
        refreshBbo();
        Side side = isBid ? Side.BID : Side.ASK;
        String key = key(side, price);
        TrackedLevel existing = active.get(key);
        if (existing != null && !existing.closed) {
            if (size <= 0) {
                existing.currentSize = 0;
                if (!existing.contacted) {
                    existing.label = EventLabel.WITHDRAW;
                }
                closeLevel(existing);
            } else {
                if (size > existing.currentSize && existing.contacted) {
                    existing.replenishCount++;
                }
                existing.currentSize = size;
                existing.peakSize = Math.max(existing.peakSize, size);
                existing.lastUpdateNs = nowNs;
            }
        } else if (size > 0 && isLarge(size) && isNearPrice(side, price)) {
            TrackedLevel level = new TrackedLevel(side, price, size, nowNs);
            classifyApproach(level);
            active.put(key, level);
        }
        updateDwellAndApproach();
    }

    public void onTrade(int priceTicks, int size, boolean buyAggressor) {
        lastTradePrice = priceTicks;
        refreshBbo();

        // Buyer aggressor hits asks; seller aggressor hits bids.
        Side hitSide = buyAggressor ? Side.ASK : Side.BID;
        for (TrackedLevel level : active.values()) {
            if (level.closed || level.side != hitSide) {
                continue;
            }
            int distance = Math.abs(priceTicks - level.price);
            boolean atOrThrough = buyAggressor
                    ? priceTicks >= level.price
                    : priceTicks <= level.price;
            if (!atOrThrough && distance > tickRadius) {
                continue;
            }
            if (!level.contacted) {
                level.contacted = true;
                level.contactNs = nowNs;
                level.midAtContact = midPrice();
            }
            level.executedInto += size;
            if (buyAggressor) {
                level.maxPenetrationTicks = Math.max(level.maxPenetrationTicks, Math.max(0, priceTicks - level.price));
            } else {
                level.maxPenetrationTicks = Math.max(level.maxPenetrationTicks, Math.max(0, level.price - priceTicks));
            }
            maybeMarkAbsorb(level);
        }
        updateDwellAndApproach();
    }

    public List<TrackedLevel> getActiveLevels() {
        List<TrackedLevel> out = new ArrayList<>();
        for (TrackedLevel level : active.values()) {
            if (!level.closed) {
                out.add(level);
            }
        }
        return out;
    }

    public List<TrackedLevel> drainClosedEvents() {
        List<TrackedLevel> out = new ArrayList<>(recentlyClosed);
        recentlyClosed.clear();
        return out;
    }

    /** Strongest current absorb / dwell score for bottom-panel plotting. */
    public double currentAbsorbScore() {
        double best = 0;
        for (TrackedLevel level : active.values()) {
            if (level.closed) {
                continue;
            }
            if (level.label == EventLabel.ABSORB || level.consumeRatio() >= 1.0) {
                best = Math.max(best, Math.min(10.0, level.consumeRatio()));
            }
        }
        return best;
    }

    public double currentMaxDwellSeconds() {
        double best = 0;
        for (TrackedLevel level : active.values()) {
            if (!level.closed) {
                best = Math.max(best, level.dwellSeconds(nowNs));
            }
        }
        return best;
    }

    public int eventCode(EventLabel label) {
        switch (label) {
            case ATTRACT: return 1;
            case REPEL: return 2;
            case ABSORB: return 3;
            case WITHDRAW: return 4;
            case DWELL_SUPPORT: return 5;
            default: return 0;
        }
    }

    private void updateDwellAndApproach() {
        Integer mid = midPrice();
        if (mid == null) {
            return;
        }
        Iterator<Map.Entry<String, TrackedLevel>> it = active.entrySet().iterator();
        while (it.hasNext()) {
            TrackedLevel level = it.next().getValue();
            if (level.closed) {
                it.remove();
                continue;
            }
            int distance = Math.abs(mid - level.price);
            if (distance <= tickRadius) {
                if (level.nearPriceSinceNs < 0) {
                    level.nearPriceSinceNs = nowNs;
                }
                if (!level.contacted && level.dwellSeconds(nowNs) >= dwellAlertSeconds) {
                    level.label = EventLabel.DWELL_SUPPORT;
                }
                // Attraction: mid moved toward the level since first seen.
                if (!level.contacted && level.label == EventLabel.NONE) {
                    classifyApproach(level);
                }
            } else if (!level.contacted && distance > tickRadius * 2) {
                // Price moved away without trading — possible repel after approach,
                // or simply left the watch radius.
                if (level.nearPriceSinceNs >= 0 && level.label != EventLabel.WITHDRAW) {
                    level.label = EventLabel.REPEL;
                    closeLevel(level);
                }
            }

            // Drop stale far levels that never became interesting.
            if (!level.contacted && distance > tickRadius * 3
                    && level.ageSeconds(nowNs) > 30) {
                it.remove();
            }
        }
    }

    private void updatePostContactMoves() {
        Integer mid = midPrice();
        if (mid == null) {
            return;
        }
        for (TrackedLevel level : active.values()) {
            if (level.closed || !level.contacted || level.midAtContact == null) {
                continue;
            }
            double elapsedSec = (nowNs - level.contactNs) / 1_000_000_000.0;
            double move = (mid - level.midAtContact);
            // Sign: positive move is up. For ask absorption we expect downward move (repel up-side).
            if (level.postMove250ms == null && elapsedSec >= 0.25) {
                level.postMove250ms = move;
            }
            if (level.postMove1s == null && elapsedSec >= 1.0) {
                level.postMove1s = move;
            }
            if (level.postMove2s == null && elapsedSec >= 2.0) {
                level.postMove2s = move;
                finalizeAfterContact(level);
            }
        }
    }

    private void finalizeAfterContact(TrackedLevel level) {
        maybeMarkAbsorb(level);
        if (level.label == EventLabel.ABSORB || level.consumeRatio() >= 0.5) {
            // Repel if price moved away from the side after absorbing.
            if (level.postMove1s != null) {
                boolean movedAway = level.side == Side.ASK
                        ? level.postMove1s < 0
                        : level.postMove1s > 0;
                if (movedAway) {
                    level.label = EventLabel.REPEL;
                } else if (level.label != EventLabel.ABSORB) {
                    level.label = EventLabel.ABSORB;
                }
            }
        }
        closeLevel(level);
    }

    private void maybeMarkAbsorb(TrackedLevel level) {
        if (level.executedInto >= Math.max(1, level.initialSize)
                && level.maxPenetrationTicks <= Math.max(1, tickRadius / 2)) {
            level.label = EventLabel.ABSORB;
        }
    }

    private void classifyApproach(TrackedLevel level) {
        Integer mid = midPrice();
        Integer trade = lastTradePrice;
        if (mid == null) {
            return;
        }
        // If last trade is closer to the level than mid was when level appeared, treat as attract.
        if (trade != null) {
            int distNow = Math.abs(trade - level.price);
            int distMid = Math.abs(mid - level.price);
            if (distNow < distMid && distNow <= tickRadius) {
                level.label = EventLabel.ATTRACT;
            }
        }
    }

    private void closeLevel(TrackedLevel level) {
        if (level.closed) {
            return;
        }
        level.closed = true;
        recentlyClosed.add(level);
    }

    private boolean isLarge(int size) {
        if (size >= absoluteSizeThreshold) {
            return true;
        }
        return depthSizeStats.size() >= 10 && depthSizeStats.zScore(size) >= zScoreThreshold;
    }

    private boolean isNearPrice(Side side, int price) {
        if (bestBid == Integer.MIN_VALUE || bestAsk == Integer.MAX_VALUE) {
            return false;
        }
        int ref = side == Side.BID ? bestBid : bestAsk;
        return Math.abs(price - ref) <= tickRadius;
    }

    private void refreshBbo() {
        bestBid = bids.isEmpty() ? Integer.MIN_VALUE : bids.firstKey();
        bestAsk = asks.isEmpty() ? Integer.MAX_VALUE : asks.firstKey();
    }

    private Integer midPrice() {
        if (bestBid == Integer.MIN_VALUE || bestAsk == Integer.MAX_VALUE) {
            return lastTradePrice;
        }
        return (bestBid + bestAsk) / 2;
    }

    private static String key(Side side, int price) {
        return side.name() + ":" + price;
    }
}
