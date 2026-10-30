package com.bookmap.orderflow.lvn;

import java.awt.Toolkit;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;

import velox.api.layer1.common.Log;

/**
 * Approach alert for displayed LVNs and/or DOP.
 *
 * <p>Rule: mid must come from outside the Approach band, enter within
 * Approach pts of the level, and not pull back ≥ Pullback pts away from the
 * level during that approach (distance increases by ≥ Pullback from the
 * closest point seen while outside). Fires on clean band entry.
 *
 * <p>Modes: single ES / single NQ / either / correlated (both markets tag
 * their own level within a time window).
 */
public final class LvnApproachAlert {

    public enum Mode {
        ES,
        NQ,
        EITHER,
        CORRELATED
    }

    public static final class Config {
        public boolean enabled = false;
        public Mode mode = Mode.CORRELATED;
        public double esApproachPts = 5.0;
        public double nqApproachPts = 20.0;
        public double esPullbackPts = 3.0;
        public double nqPullbackPts = 10.0;
        public int cooldownSec = 60;
        public int corrWindowSec = 30;
        public boolean watchLvn = true;
        public boolean watchDop = true;
        public boolean sound = true;

        public Config copy() {
            Config c = new Config();
            c.enabled = enabled;
            c.mode = mode == null ? Mode.CORRELATED : mode;
            c.esApproachPts = esApproachPts;
            c.nqApproachPts = nqApproachPts;
            c.esPullbackPts = esPullbackPts;
            c.nqPullbackPts = nqPullbackPts;
            c.cooldownSec = cooldownSec;
            c.corrWindowSec = corrWindowSec;
            c.watchLvn = watchLvn;
            c.watchDop = watchDop;
            c.sound = sound;
            return c;
        }
    }

    public static final class Tag {
        public final String market;
        public final String kind; // LVN / DOP
        public final double levelPx;
        public final double midPx;
        /** +1 from below (into resistance), −1 from above (into support). */
        public final int approachSide;
        public final long tsNs;

        Tag(String market, String kind, double levelPx, double midPx, int approachSide, long tsNs) {
            this.market = market;
            this.kind = kind;
            this.levelPx = levelPx;
            this.midPx = midPx;
            this.approachSide = approachSide;
            this.tsNs = tsNs;
        }
    }

    public static final class Event {
        public final boolean correlated;
        public final Tag primary;
        public final Tag partner; // non-null when correlated
        public final String message;

        Event(boolean correlated, Tag primary, Tag partner, String message) {
            this.correlated = correlated;
            this.primary = primary;
            this.partner = partner;
            this.message = message;
        }
    }

    private static final class Probe {
        final String market;
        final String kind;
        double levelPx;
        /** True once mid has been strictly outside the Approach band since last fire/reset. */
        boolean seenOutside;
        /** Last sample was inside the Approach band (used to reset on leave after a dirty reject). */
        boolean wasInside;
        boolean dirty;
        double minDistOutside = Double.POSITIVE_INFINITY;
        long cooldownUntilNs;

        Probe(String market, String kind, double levelPx) {
            this.market = market;
            this.kind = kind;
            this.levelPx = levelPx;
        }
    }

    private Config cfg = new Config();
    private final Map<String, Probe> probes = new HashMap<>();
    private Tag pendingEs;
    private Tag pendingNq;
    private Consumer<Event> listener;

    public synchronized void setListener(Consumer<Event> listener) {
        this.listener = listener;
    }

    public synchronized Config config() {
        return cfg.copy();
    }

    public synchronized void applyConfig(Config in) {
        if (in == null) {
            return;
        }
        Config next = in.copy();
        next.esApproachPts = Math.max(0.25, next.esApproachPts);
        next.nqApproachPts = Math.max(0.25, next.nqApproachPts);
        next.esPullbackPts = Math.max(0.0, next.esPullbackPts);
        next.nqPullbackPts = Math.max(0.0, next.nqPullbackPts);
        next.cooldownSec = Math.max(0, Math.min(3600, next.cooldownSec));
        next.corrWindowSec = Math.max(1, Math.min(600, next.corrWindowSec));
        if (next.mode == null) {
            next.mode = Mode.CORRELATED;
        }
        this.cfg = next;
        if (!next.enabled) {
            pendingEs = null;
            pendingNq = null;
            for (Probe p : probes.values()) {
                p.seenOutside = false;
                p.dirty = false;
                p.minDistOutside = Double.POSITIVE_INFINITY;
            }
        }
    }

    public synchronized void resetSession() {
        probes.clear();
        pendingEs = null;
        pendingNq = null;
    }

    /**
     * Feed latest mid for a market. Levels are read from {@link LvnLevelsState}.
     */
    public synchronized void onMid(String market, double mid, long nowNs) {
        if (!cfg.enabled || market == null || !Double.isFinite(mid) || mid <= 0 || nowNs <= 0) {
            return;
        }
        String mkt = "NQ".equalsIgnoreCase(market) ? "NQ" : "ES";
        if (cfg.mode == Mode.ES && !"ES".equals(mkt)) {
            // still track for correlated pending cleanup? no — skip entirely
            return;
        }
        if (cfg.mode == Mode.NQ && !"NQ".equals(mkt)) {
            return;
        }

        syncProbes(mkt);
        expirePendings(nowNs);

        double approachPts = "NQ".equals(mkt) ? cfg.nqApproachPts : cfg.esApproachPts;
        double pullbackPts = "NQ".equals(mkt) ? cfg.nqPullbackPts : cfg.esPullbackPts;
        long coolNs = cfg.cooldownSec * 1_000_000_000L;

        List<Tag> fired = new ArrayList<>(2);
        for (Probe p : probes.values()) {
            if (!p.market.equals(mkt)) {
                continue;
            }
            Tag t = updateProbe(p, mid, nowNs, approachPts, pullbackPts, coolNs);
            if (t != null) {
                fired.add(t);
            }
        }
        for (Tag t : fired) {
            routeTag(t, nowNs);
        }
    }

    private void syncProbes(String market) {
        LvnLevelsState.MarketLevels lv = LvnLevelsState.levelsFor(market);
        Set<String> keep = new HashSet<>();
        if (cfg.watchLvn && lv != null) {
            addLevelProbes(market, "LVN", lv.lvnAbove, keep);
            addLevelProbes(market, "LVN", lv.lvnBelow, keep);
        }
        if (cfg.watchDop) {
            double dop = LvnLevelsState.dopFor(market);
            if (Double.isFinite(dop) && dop > 0) {
                String key = keyOf(market, "DOP", dop);
                keep.add(key);
                probes.computeIfAbsent(key, k -> new Probe(market, "DOP", dop)).levelPx = dop;
            }
        }
        Iterator<Map.Entry<String, Probe>> it = probes.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, Probe> e = it.next();
            if (e.getValue().market.equals(market) && !keep.contains(e.getKey())) {
                it.remove();
            }
        }
    }

    private void addLevelProbes(String market, String kind, List<Double> prices, Set<String> keep) {
        if (prices == null) {
            return;
        }
        for (Double px : prices) {
            if (px == null || !Double.isFinite(px) || px <= 0) {
                continue;
            }
            String key = keyOf(market, kind, px);
            keep.add(key);
            probes.computeIfAbsent(key, k -> new Probe(market, kind, px)).levelPx = px;
        }
    }

    private static String keyOf(String market, String kind, double px) {
        long ticks = Math.round(px * 4.0); // 0.25 grid
        return market + "|" + kind + "|" + ticks;
    }

    private Tag updateProbe(Probe p, double mid, long nowNs, double approachPts, double pullbackPts, long coolNs) {
        if (nowNs < p.cooldownUntilNs) {
            return null;
        }
        double dist = Math.abs(mid - p.levelPx);
        if (dist > approachPts) {
            if (p.wasInside || !p.seenOutside) {
                // Fresh outside leg (start, or leave after reject / prior inside).
                p.seenOutside = true;
                p.wasInside = false;
                p.dirty = false;
                p.minDistOutside = dist;
            } else if (dist + 1e-9 < p.minDistOutside) {
                p.minDistOutside = dist;
            } else if (pullbackPts > 0 && dist - p.minDistOutside >= pullbackPts - 1e-9) {
                // Pulled back away from the level by ≥ Pullback while approaching.
                p.dirty = true;
                p.minDistOutside = dist;
            }
            return null;
        }

        // Inside Approach band.
        p.wasInside = true;
        if (!p.seenOutside) {
            // Started already inside — require a leave before arming.
            return null;
        }
        if (p.dirty) {
            // Need to leave the band and re-approach cleanly.
            return null;
        }

        int side = mid < p.levelPx ? 1 : -1;
        Tag tag = new Tag(p.market, p.kind, p.levelPx, mid, side, nowNs);
        p.cooldownUntilNs = nowNs + coolNs;
        p.seenOutside = false;
        p.wasInside = true;
        p.dirty = false;
        p.minDistOutside = Double.POSITIVE_INFINITY;
        return tag;
    }

    private void expirePendings(long nowNs) {
        long winNs = cfg.corrWindowSec * 1_000_000_000L;
        if (pendingEs != null && nowNs - pendingEs.tsNs > winNs) {
            if (cfg.mode == Mode.CORRELATED) {
                // expired unpaired — drop silently
            }
            pendingEs = null;
        }
        if (pendingNq != null && nowNs - pendingNq.tsNs > winNs) {
            pendingNq = null;
        }
    }

    private void routeTag(Tag tag, long nowNs) {
        Mode mode = cfg.mode;
        if (mode == Mode.ES && !"ES".equals(tag.market)) {
            return;
        }
        if (mode == Mode.NQ && !"NQ".equals(tag.market)) {
            return;
        }

        if (mode == Mode.CORRELATED) {
            if ("ES".equals(tag.market)) {
                pendingEs = tag;
            } else {
                pendingNq = tag;
            }
            if (pendingEs != null && pendingNq != null
                    && Math.abs(pendingEs.tsNs - pendingNq.tsNs)
                    <= cfg.corrWindowSec * 1_000_000_000L) {
                emitCorr(pendingEs, pendingNq);
                pendingEs = null;
                pendingNq = null;
            }
            return;
        }

        // Single / Either — fire immediately; also keep pending for a bonus corr log.
        emitSingle(tag);
        if ("ES".equals(tag.market)) {
            pendingEs = tag;
        } else {
            pendingNq = tag;
        }
        if (pendingEs != null && pendingNq != null
                && Math.abs(pendingEs.tsNs - pendingNq.tsNs)
                <= cfg.corrWindowSec * 1_000_000_000L) {
            emitCorr(pendingEs, pendingNq);
            pendingEs = null;
            pendingNq = null;
        }
    }

    private void emitSingle(Tag t) {
        String dir = t.approachSide > 0 ? "from-below" : "from-above";
        String msg = String.format(Locale.US,
                "APPROACH %s %s=%.2f mid=%.2f %s",
                t.market, t.kind, t.levelPx, t.midPx, dir);
        emit(new Event(false, t, null, msg));
    }

    private void emitCorr(Tag a, Tag b) {
        Tag es = "ES".equals(a.market) ? a : b;
        Tag nq = "NQ".equals(a.market) ? a : b;
        String msg = String.format(Locale.US,
                "APPROACH CORRELATED ES %s=%.2f mid=%.2f + NQ %s=%.2f mid=%.2f",
                es.kind, es.levelPx, es.midPx, nq.kind, nq.levelPx, nq.midPx);
        emit(new Event(true, es, nq, msg));
    }

    private void emit(Event ev) {
        Log.info("LVN Levels " + ev.message);
        if (cfg.sound) {
            try {
                Toolkit.getDefaultToolkit().beep();
            } catch (Throwable ignored) {
                // headless / restricted
            }
        }
        Consumer<Event> l = listener;
        if (l != null) {
            try {
                l.accept(ev);
            } catch (RuntimeException ex) {
                Log.info("LVN Levels alert listener failed: " + ex.getMessage());
            }
        }
    }
}
