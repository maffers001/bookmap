package com.bookmap.orderflow.core;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Seeded volume-at-price with optional Bookmap live accumulate and sliding window.
 *
 * <p>Seed supplies whole-day maps + through-day minutes. Live trades after
 * {@code joinThrough} append to a minute deque. {@link #rebuild(int)} slices the
 * last {@code windowDays} and returns a dense row→vol map for detection.
 */
public final class LvnLiveVap {

    public static final class NodesSnapshot {
        public final List<LvnNodeDetector.Node> valleys;
        public final List<LvnNodeDetector.Node> peaks;
        public final Map<Integer, Double> vap;
        public final int windowDaysUsed;
        public final int sensitivity;
        public final LocalDateTime asOf;
        public final String warning;

        public NodesSnapshot(
                List<LvnNodeDetector.Node> valleys,
                List<LvnNodeDetector.Node> peaks,
                Map<Integer, Double> vap,
                int windowDaysUsed,
                int sensitivity,
                LocalDateTime asOf,
                String warning) {
            this.valleys = valleys;
            this.peaks = peaks;
            this.vap = vap;
            this.windowDaysUsed = windowDaysUsed;
            this.sensitivity = sensitivity;
            this.asOf = asOf;
            this.warning = warning == null ? "" : warning;
        }
    }

    private LvnVapSeedFile.Seed seed;
    private LocalDateTime joinThrough;
    /** Epoch-ns equivalent of {@link #joinThrough} for the hot trade path. */
    private long joinNs = Long.MIN_VALUE;
    private double tick = LvnNodeDetector.DEFAULT_TICK;

    /** Live minutes after join (mutable maps). */
    private final Deque<LiveMinute> liveMinutes = new ArrayDeque<>();
    private LocalDateTime currentMinute;
    private long currentMinuteEpoch = Long.MIN_VALUE;
    private Map<Integer, Double> currentMap = new HashMap<>();

    private LocalDateTime firstLivePrint;
    private LocalDateTime lastLivePrint;
    private long liveTradeCount;

    private static final class LiveMinute {
        final LocalDateTime minute;
        final Map<Integer, Double> vol;

        LiveMinute(LocalDateTime minute, Map<Integer, Double> vol) {
            this.minute = minute;
            this.vol = vol;
        }
    }

    public void loadSeed(LvnVapSeedFile.Seed s) {
        this.seed = s;
        this.tick = s != null && s.meta.tick > 0 ? s.meta.tick : LvnNodeDetector.DEFAULT_TICK;
        setJoinThroughInternal(s != null ? s.meta.throughUk : null);
        clearLive();
    }

    public void clear() {
        seed = null;
        joinThrough = null;
        joinNs = Long.MIN_VALUE;
        clearLive();
    }

    public void clearLive() {
        liveMinutes.clear();
        currentMinute = null;
        currentMinuteEpoch = Long.MIN_VALUE;
        currentMap = new HashMap<>();
        firstLivePrint = null;
        lastLivePrint = null;
        liveTradeCount = 0;
    }

    public boolean hasSeed() {
        return seed != null;
    }

    public LvnVapSeedFile.Seed seed() {
        return seed;
    }

    public LocalDateTime joinThrough() {
        return joinThrough;
    }

    /** Override join gate (UK local). */
    public void setJoinThrough(LocalDateTime through) {
        setJoinThroughInternal(through);
    }

    private void setJoinThroughInternal(LocalDateTime through) {
        this.joinThrough = through;
        this.joinNs = through == null ? Long.MIN_VALUE
                : through.atZone(LvnStackFile.UK).toInstant().toEpochMilli() * 1_000_000L;
    }

    public long joinNs() {
        return joinNs;
    }

    public double tick() {
        return tick;
    }

    public long liveTradeCount() {
        return liveTradeCount;
    }

    public LocalDateTime firstLivePrint() {
        return firstLivePrint;
    }

    public LocalDateTime lastLivePrint() {
        return lastLivePrint;
    }

    /**
     * Accumulate a Bookmap trade if {@code tsUk >= joinThrough}.
     *
     * @param priceTick absolute tick index (price / pips)
     * @param size trade size
     * @param tsUk UK local timestamp of the trade
     * @return true if accepted
     */
    public boolean onTrade(int priceTick, int size, LocalDateTime tsUk) {
        if (size <= 0 || tsUk == null) {
            return false;
        }
        long ns = tsUk.atZone(LvnStackFile.UK).toInstant().toEpochMilli() * 1_000_000L;
        return onTradeNs(priceTick, size, ns);
    }

    /**
     * Hot-path accumulate using feed epoch nanos — no date/time conversion
     * per trade (minute {@link LocalDateTime} is derived once per minute).
     */
    public boolean onTradeNs(int priceTick, int size, long tsNs) {
        if (size <= 0) {
            return false;
        }
        if (joinNs != Long.MIN_VALUE && tsNs < joinNs) {
            return false;
        }
        long minuteEpoch = Math.floorDiv(tsNs, 60_000_000_000L);
        if (currentMinute == null) {
            currentMinuteEpoch = minuteEpoch;
            currentMinute = minuteFromEpoch(minuteEpoch);
            currentMap = new HashMap<>();
        } else if (minuteEpoch != currentMinuteEpoch) {
            liveMinutes.addLast(new LiveMinute(currentMinute, currentMap));
            currentMinuteEpoch = minuteEpoch;
            currentMinute = minuteFromEpoch(minuteEpoch);
            currentMap = new HashMap<>();
        }
        currentMap.merge(priceTick, (double) size, Double::sum);
        liveTradeCount++;
        // Minute-precision timestamps are enough for gap warnings / as-of.
        if (firstLivePrint == null) {
            firstLivePrint = currentMinute;
        }
        lastLivePrint = currentMinute;
        return true;
    }

    private static LocalDateTime minuteFromEpoch(long minuteEpoch) {
        long sec = minuteEpoch * 60L;
        return LocalDateTime.ofInstant(
                java.time.Instant.ofEpochSecond(sec), LvnStackFile.UK);
    }

    /**
     * Build VAP for the trailing {@code windowDays} ending at {@code asOf}
     * (or seed through / last live print), then detect nodes.
     */
    public NodesSnapshot rebuild(int windowDays, int sensitivity, LocalDateTime asOf) {
        Map<Integer, Double> vap = new TreeMap<>();
        String warning = "";
        LocalDateTime end = asOf;
        if (end == null) {
            if (lastLivePrint != null) {
                end = lastLivePrint;
            } else if (joinThrough != null) {
                end = joinThrough;
            } else {
                end = LocalDateTime.now(LvnStackFile.UK);
            }
        }
        LocalDate endDay = end.toLocalDate();
        int want = Math.max(1, windowDays);
        LocalDate startDay = endDay.minusDays(want - 1L);

        int availableDays = 0;
        if (seed != null) {
            for (Map.Entry<LocalDate, Map<Integer, Double>> e : seed.daily.entrySet()) {
                LocalDate d = e.getKey();
                if (d.isBefore(startDay) || d.isAfter(endDay)) {
                    continue;
                }
                // Whole days strictly before end day, or before through if end is through day
                if (d.equals(endDay) && joinThrough != null
                        && end.toLocalTime().equals(joinThrough.toLocalTime())
                        && liveTradeCount == 0) {
                    // end day covered by seed minutes instead
                    continue;
                }
                if (d.equals(endDay)) {
                    // End day volume comes from seed minutes + live, not full daily
                    continue;
                }
                availableDays++;
                addMap(vap, e.getValue());
            }
            LocalDateTime minuteCutoff = startDay.atStartOfDay();
            for (LvnVapSeedFile.MinuteBucket mb : seed.minutes) {
                if (mb.minute.isBefore(minuteCutoff) || mb.minute.isAfter(end)) {
                    continue;
                }
                addMap(vap, mb.vol);
            }
        }

        // Live minutes (+ open bucket)
        List<LiveMinute> allLive = new ArrayList<>(liveMinutes);
        if (currentMinute != null && !currentMap.isEmpty()) {
            allLive.add(new LiveMinute(currentMinute, currentMap));
        }
        for (LiveMinute lm : allLive) {
            if (lm.minute.isBefore(startDay.atStartOfDay()) || lm.minute.isAfter(end)) {
                continue;
            }
            addMap(vap, lm.vol);
        }

        if (seed != null) {
            LocalDate earliest = null;
            for (LocalDate d : seed.daily.keySet()) {
                if (earliest == null || d.isBefore(earliest)) {
                    earliest = d;
                }
            }
            for (LvnVapSeedFile.MinuteBucket mb : seed.minutes) {
                LocalDate d = mb.minute.toLocalDate();
                if (earliest == null || d.isBefore(earliest)) {
                    earliest = d;
                }
            }
            if (earliest != null && earliest.isAfter(startDay)) {
                warning = "seed short: available from " + earliest + " (wanted " + startDay + ")";
                // clamp reported window
                long have = java.time.temporal.ChronoUnit.DAYS.between(earliest, endDay) + 1;
                want = (int) Math.min(want, Math.max(1, have));
            }
        }

        if (seed != null) {
            LocalDate lastSeedDay = null;
            for (LocalDate d : seed.daily.keySet()) {
                if (lastSeedDay == null || d.isAfter(lastSeedDay)) {
                    lastSeedDay = d;
                }
            }
            for (LvnVapSeedFile.MinuteBucket mb : seed.minutes) {
                LocalDate d = mb.minute.toLocalDate();
                if (lastSeedDay == null || d.isAfter(lastSeedDay)) {
                    lastSeedDay = d;
                }
            }
            if (lastSeedDay != null && endDay.isAfter(lastSeedDay)) {
                long gap = java.time.temporal.ChronoUnit.DAYS.between(lastSeedDay, endDay);
                if (!warning.isEmpty()) {
                    warning += " | ";
                }
                warning += "STALE SEED ends " + lastSeedDay + " · session " + endDay
                        + " (missing " + gap + "d — rebuild seed --day " + endDay + ")";
            }
        }

        if (joinThrough != null && firstLivePrint != null && firstLivePrint.isAfter(joinThrough)) {
            if (!warning.isEmpty()) {
                warning += " | ";
            }
            warning += "gap join→firstPrint " + joinThrough.toLocalTime()
                    + "→" + firstLivePrint.toLocalTime();
        }

        List<LvnNodeDetector.Node>[] nodes = LvnNodeDetector.detect(vap, sensitivity, tick, true, true);
        return new NodesSnapshot(nodes[0], nodes[1], vap, want, sensitivity, end, warning);
    }

    private static void addMap(Map<Integer, Double> dest, Map<Integer, Double> src) {
        if (src == null) {
            return;
        }
        for (Map.Entry<Integer, Double> e : src.entrySet()) {
            if (e.getValue() != null && e.getValue() > 0) {
                dest.merge(e.getKey(), e.getValue(), Double::sum);
            }
        }
    }

    /** UK minutes-of-day helper. */
    public static LocalTime parseUkHm(String hm) {
        if (hm == null || hm.isBlank()) {
            return LocalTime.of(13, 55);
        }
        String[] p = hm.trim().split(":");
        int h = Integer.parseInt(p[0]);
        int m = p.length > 1 ? Integer.parseInt(p[1]) : 0;
        return LocalTime.of(h, m);
    }
}
