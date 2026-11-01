package com.bookmap.orderflow.lvn;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.SourceDataLine;

import velox.api.layer1.common.Log;

/**
 * Approach alert for displayed LVNs and/or DOP.
 *
 * <p>Rule: mid must come from outside the Approach band, enter within
 * Approach pts of the level, and not pull back ≥ Pullback pts away from the
 * level during that approach (distance increases by ≥ Pullback from the
 * closest point seen while outside). Fires on clean band entry.
 *
 * <p>Modes: single ES / single NQ / either / correlated. Correlated is for
 * synchronised S/R: one market clean-enters, the other is within Prox of a
 * watched level, <b>both share the same approach side</b> (both from-below or
 * both from-above), both mids are still within their Prox of their levels,
 * and neither has already traded through its level.
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
        /** Partner ES mid must be within this many pts of an ES LVN/DOP. */
        public double esCorrProxPts = 5.0;
        /** Partner NQ mid must be within this many pts of an NQ LVN/DOP. */
        public double nqCorrProxPts = 20.0;
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
            c.esCorrProxPts = esCorrProxPts;
            c.nqCorrProxPts = nqCorrProxPts;
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
        /** Short footer line, e.g. {@code ES cross DOP, NQ approach LVN}. */
        public final String reason;

        Event(boolean correlated, Tag primary, Tag partner, String message, String reason) {
            this.correlated = correlated;
            this.primary = primary;
            this.partner = partner;
            this.message = message;
            this.reason = reason == null ? "" : reason;
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
        /**
         * Side while arming from outside: +1 from below (into resistance),
         * −1 from above (into support). Kept through band entry so a wick
         * through the level does not flip the rotation label.
         */
        int armedSide;
        long cooldownUntilNs;

        Probe(String market, String kind, double levelPx) {
            this.market = market;
            this.kind = kind;
            this.levelPx = levelPx;
        }
    }

    private Config cfg = new Config();
    private final Map<String, Probe> probes = new HashMap<>();
    private double lastMidEs = Double.NaN;
    private double lastMidNq = Double.NaN;
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
        next.esCorrProxPts = Math.max(0.25, next.esCorrProxPts);
        next.nqCorrProxPts = Math.max(0.25, next.nqCorrProxPts);
        if (next.mode == null) {
            next.mode = Mode.CORRELATED;
        }
        boolean wasOn = this.cfg.enabled;
        this.cfg = next;
        if (!next.enabled) {
            probes.clear();
        } else if (!wasOn) {
            // Fresh arm: drop stale probe state so the next outside→inside can fire.
            probes.clear();
        }
    }

    public synchronized void resetSession() {
        probes.clear();
        lastMidEs = Double.NaN;
        lastMidNq = Double.NaN;
    }

    /**
     * Feed latest mid for a market. Levels are read from {@link LvnLevelsState}.
     */
    public synchronized void onMid(String market, double mid, long nowNs) {
        if (!cfg.enabled || market == null || !Double.isFinite(mid) || mid <= 0 || nowNs <= 0) {
            return;
        }
        String mkt = "NQ".equalsIgnoreCase(market) ? "NQ" : "ES";
        if ("NQ".equals(mkt)) {
            lastMidNq = mid;
        } else {
            lastMidEs = mid;
        }
        if (cfg.mode == Mode.ES && !"ES".equals(mkt)) {
            return;
        }
        if (cfg.mode == Mode.NQ && !"NQ".equals(mkt)) {
            return;
        }

        syncProbes(mkt);

        double approachPts = "NQ".equals(mkt) ? cfg.nqApproachPts : cfg.esApproachPts;
        double pullbackPts = "NQ".equals(mkt) ? cfg.nqPullbackPts : cfg.esPullbackPts;

        List<Tag> fired = new ArrayList<>(2);
        for (Probe p : probes.values()) {
            if (!p.market.equals(mkt)) {
                continue;
            }
            Tag t = updateProbe(p, mid, nowNs, approachPts, pullbackPts);
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
        if (lv != null && cfg.watchLvn) {
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

    private Tag updateProbe(Probe p, double mid, long nowNs, double approachPts, double pullbackPts) {
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
                p.armedSide = mid < p.levelPx ? 1 : -1;
            } else if (dist + 1e-9 < p.minDistOutside) {
                p.minDistOutside = dist;
                p.armedSide = mid < p.levelPx ? 1 : -1;
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
        if (p.armedSide == 0) {
            p.armedSide = mid < p.levelPx ? 1 : -1;
        }

        Tag tag = new Tag(p.market, p.kind, p.levelPx, mid, p.armedSide, nowNs);
        // Consume this approach leg (must leave band before another attempt).
        // Cooldown is applied only if the alert actually emits.
        p.seenOutside = false;
        p.wasInside = true;
        p.dirty = false;
        p.minDistOutside = Double.POSITIVE_INFINITY;
        return tag;
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
            // Live mid for the primary (tag.mid is the entry print).
            double primaryMid = "NQ".equals(tag.market) ? lastMidNq : lastMidEs;
            if (!Double.isFinite(primaryMid) || primaryMid <= 0) {
                primaryMid = tag.midPx;
            }
            Tag primary = new Tag(tag.market, tag.kind, tag.levelPx, primaryMid,
                    tag.approachSide, tag.tsNs);
            double primaryProx = "NQ".equals(primary.market) ? cfg.nqCorrProxPts : cfg.esCorrProxPts;
            if (!withinProx(primary, primaryProx) || isThroughLevel(primary)) {
                // Already run away from / through the level — not sync S/R.
                return;
            }

            String other = "ES".equals(tag.market) ? "NQ" : "ES";
            double otherMid = "NQ".equals(other) ? lastMidNq : lastMidEs;
            double proxPts = "NQ".equals(other) ? cfg.nqCorrProxPts : cfg.esCorrProxPts;
            Tag partner = nearestWatchedSameSide(other, otherMid, proxPts, primary.approachSide, nowNs);
            if (partner == null || !withinProx(partner, proxPts) || isThroughLevel(partner)) {
                return;
            }
            emitCorr(primary, partner);
            applyCooldown(tag, nowNs);
            return;
        }

        emitSingle(tag);
        applyCooldown(tag, nowNs);
    }

    private static boolean withinProx(Tag t, double proxPts) {
        return t != null && Math.abs(t.midPx - t.levelPx) <= proxPts + 1e-9;
    }

    /**
     * True if mid has already broken through the level relative to the armed
     * approach side (from-below → mid above level; from-above → mid below).
     */
    private static boolean isThroughLevel(Tag t) {
        if (t == null) {
            return true;
        }
        if (t.approachSide > 0) {
            return t.midPx > t.levelPx + 1e-9;
        }
        return t.midPx < t.levelPx - 1e-9;
    }

    /**
     * Closest watched LVN/DOP for {@code market} with |mid − level| ≤ maxPts
     * and mid still on {@code requireSide} of that level.
     */
    private Tag nearestWatchedSameSide(
            String market, double mid, double maxPts, int requireSide, long nowNs) {
        if (!Double.isFinite(mid) || mid <= 0 || maxPts < 0 || requireSide == 0) {
            return null;
        }
        String bestKind = null;
        double bestPx = Double.NaN;
        double bestDist = maxPts;
        if (cfg.watchLvn) {
            LvnLevelsState.MarketLevels lv = LvnLevelsState.levelsFor(market);
            if (lv != null) {
                if (lv.lvnAbove != null) {
                    for (Double px : lv.lvnAbove) {
                        if (px == null || !Double.isFinite(px) || px <= 0 || !sideMatch(mid, px, requireSide)) {
                            continue;
                        }
                        double d = Math.abs(mid - px);
                        if (d <= bestDist + 1e-9) {
                            bestKind = "LVN";
                            bestPx = px;
                            bestDist = d;
                        }
                    }
                }
                if (lv.lvnBelow != null) {
                    for (Double px : lv.lvnBelow) {
                        if (px == null || !Double.isFinite(px) || px <= 0 || !sideMatch(mid, px, requireSide)) {
                            continue;
                        }
                        double d = Math.abs(mid - px);
                        if (d <= bestDist + 1e-9) {
                            bestKind = "LVN";
                            bestPx = px;
                            bestDist = d;
                        }
                    }
                }
            }
        }
        if (cfg.watchDop) {
            double dop = LvnLevelsState.dopFor(market);
            if (Double.isFinite(dop) && dop > 0 && sideMatch(mid, dop, requireSide)) {
                double d = Math.abs(mid - dop);
                if (d <= bestDist + 1e-9) {
                    bestKind = "DOP";
                    bestPx = dop;
                }
            }
        }
        if (bestKind == null || !Double.isFinite(bestPx)) {
            return null;
        }
        return new Tag(market, bestKind, bestPx, mid, requireSide, nowNs);
    }

    /** Mid still on the required side of level (or exactly at it). */
    private static boolean sideMatch(double mid, double level, int requireSide) {
        if (requireSide > 0) {
            return mid <= level + 1e-9;
        }
        return mid >= level - 1e-9;
    }

    private void applyCooldown(Tag tag, long nowNs) {
        if (tag == null || cfg.cooldownSec <= 0) {
            return;
        }
        Probe p = probes.get(keyOf(tag.market, tag.kind, tag.levelPx));
        if (p != null) {
            p.cooldownUntilNs = nowNs + cfg.cooldownSec * 1_000_000_000L;
        }
    }

    private void emitSingle(Tag t) {
        String dir = t.approachSide > 0 ? "from-below" : "from-above";
        String msg = String.format(Locale.US,
                "APPROACH %s %s=%.2f mid=%.2f %s",
                t.market, t.kind, t.levelPx, t.midPx, dir);
        String reason = t.market + " cross " + t.kind;
        emit(new Event(false, t, null, msg, reason));
    }

    private void emitCorr(Tag crossed, Tag partner) {
        Tag es = "ES".equals(crossed.market) ? crossed : partner;
        Tag nq = "NQ".equals(crossed.market) ? crossed : partner;
        String msg = String.format(Locale.US,
                "APPROACH CORRELATED %s %s=%.2f mid=%.2f | partner %s %s=%.2f mid=%.2f (prox)",
                crossed.market, crossed.kind, crossed.levelPx, crossed.midPx,
                partner.market, partner.kind, partner.levelPx, partner.midPx);
        String dir = crossed.approachSide > 0 ? "from-below" : "from-above";
        String reason = crossed.market + " cross " + crossed.kind
                + ", " + partner.market + " approach " + partner.kind
                + " (" + dir + ")";
        emit(new Event(true, es, nq, msg, reason));
    }

    private void emit(Event ev) {
        Log.info("LVN Levels " + ev.message);
        if (cfg.sound) {
            playAlertSound();
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

    /**
     * Loud two-tone chirp (not the quiet OS beep). Runs off the feed thread.
     */
    private static void playAlertSound() {
        Thread t = new Thread(() -> {
            try {
                // High / higher / high — ~0.9s total, hard to miss over market noise.
                double[] freqs = {880.0, 1320.0, 880.0, 1320.0, 1760.0};
                int[] ms = {140, 140, 140, 140, 220};
                for (int i = 0; i < freqs.length; i++) {
                    playTone(freqs[i], ms[i]);
                    Thread.sleep(40L);
                }
            } catch (Throwable ignored) {
                // no audio device / interrupted
            }
        }, "LvnApproachBeep");
        t.setDaemon(true);
        t.start();
    }

    private static void playTone(double freqHz, int durationMs) throws Exception {
        float sampleRate = 22050f;
        int n = Math.max(1, (int) (sampleRate * durationMs / 1000.0));
        byte[] buf = new byte[n];
        double amp = 0.85;
        for (int i = 0; i < n; i++) {
            // Short attack/release so it doesn't click; full mid amplitude.
            double env = 1.0;
            int edge = Math.min(80, n / 8);
            if (i < edge) {
                env = i / (double) edge;
            } else if (i > n - edge) {
                env = (n - i) / (double) edge;
            }
            double sample = Math.sin(2.0 * Math.PI * freqHz * i / sampleRate) * amp * env;
            buf[i] = (byte) Math.round(sample * 127.0);
        }
        AudioFormat fmt = new AudioFormat(sampleRate, 8, 1, true, false);
        try (SourceDataLine line = AudioSystem.getSourceDataLine(fmt)) {
            line.open(fmt);
            line.start();
            line.write(buf, 0, buf.length);
            line.drain();
            line.stop();
        }
    }
}
