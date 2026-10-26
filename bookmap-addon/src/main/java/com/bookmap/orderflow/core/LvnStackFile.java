package com.bookmap.orderflow.core;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * Developing LVN stack from research {@code build_lvn_stack.py}.
 *
 * <p>CSV columns: {@code date,time_uk,price,node_volume,kind,mid,n_nodes}
 * (one row per valley per snapshot). Snapshots are typically every 1–5 minutes.
 */
public final class LvnStackFile {

    public static final ZoneId UK = ZoneId.of("Europe/London");
    public static final int MAX_DISPLAY_EACH_SIDE = 6;

    public static final class Snapshot {
        public final LocalDateTime ts;
        public final List<Double> prices; // sorted ascending
        public final double mid;

        public Snapshot(LocalDateTime ts, List<Double> prices, double mid) {
            this.ts = ts;
            this.prices = Collections.unmodifiableList(new ArrayList<>(prices));
            this.mid = mid;
        }
    }

    public static final class Stack {
        public final Path source;
        public final List<Snapshot> snapshots; // chronological

        public Stack(Path source, List<Snapshot> snapshots) {
            this.source = source;
            this.snapshots = Collections.unmodifiableList(new ArrayList<>(snapshots));
        }

        public boolean isEmpty() {
            return snapshots.isEmpty();
        }

        /** Last snapshot with {@code ts <= asOf} (UK local). */
        public Snapshot asOf(LocalDateTime asOf) {
            if (snapshots.isEmpty() || asOf == null) {
                return null;
            }
            Snapshot best = null;
            for (Snapshot s : snapshots) {
                if (!s.ts.isAfter(asOf)) {
                    best = s;
                } else {
                    break;
                }
            }
            return best;
        }

        /** Convenience: as-of from feed epoch nanos (UK zone). */
        public Snapshot asOfNs(long tsNs) {
            LocalDateTime asOf = LocalDateTime.ofInstant(
                    java.time.Instant.ofEpochSecond(tsNs / 1_000_000_000L, tsNs % 1_000_000_000L),
                    UK);
            return asOf(asOf);
        }

        /**
         * Nearest {@code nAbove} prices strictly above mid and {@code nBelow}
         * strictly below, from the as-of snapshot (or last if asOf null).
         */
        public List<Double> selectAround(
                LocalDateTime asOf, double mid, int nAbove, int nBelow) {
            Snapshot s = asOf != null ? asOf(asOf) : (snapshots.isEmpty() ? null
                    : snapshots.get(snapshots.size() - 1));
            if (s == null) {
                return List.of();
            }
            double ref = mid > 0 ? mid : s.mid;
            List<Double> below = new ArrayList<>();
            List<Double> above = new ArrayList<>();
            for (double px : s.prices) {
                if (px < ref - 1e-9) {
                    below.add(px);
                } else if (px > ref + 1e-9) {
                    above.add(px);
                }
            }
            // nearest below = highest below; nearest above = lowest above
            Collections.sort(below);
            Collections.reverse(below); // high→low so index 0 is nearest
            // above already ascending → index 0 nearest
            List<Double> out = new ArrayList<>();
            for (int i = 0; i < Math.min(nBelow, below.size()); i++) {
                out.add(below.get(i));
            }
            for (int i = 0; i < Math.min(nAbove, above.size()); i++) {
                out.add(above.get(i));
            }
            Collections.sort(out);
            return out;
        }

        public List<Double> allPricesAsOf(LocalDateTime asOf) {
            Snapshot s = asOf(asOf);
            return s == null ? List.of() : s.prices;
        }
    }

    public static final class ParseException extends Exception {
        public ParseException(String message) {
            super(message);
        }
    }

    private LvnStackFile() {}

    public static Stack load(Path path) throws IOException, ParseException {
        if (path == null || !Files.isRegularFile(path)) {
            throw new ParseException("LVN stack file not found: " + path);
        }
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        if (lines.isEmpty()) {
            throw new ParseException("Empty LVN stack: " + path.getFileName());
        }
        String header = lines.get(0).toLowerCase(Locale.ROOT);
        if (!header.contains("date") || !header.contains("price")) {
            throw new ParseException("LVN stack missing date/price header: " + path.getFileName());
        }
        String[] cols = splitCsv(lines.get(0));
        int dateIdx = indexOf(cols, "date");
        int timeIdx = indexOf(cols, "time_uk");
        if (timeIdx < 0) {
            timeIdx = indexOf(cols, "time");
        }
        int priceIdx = indexOf(cols, "price");
        int midIdx = indexOf(cols, "mid");
        if (dateIdx < 0 || timeIdx < 0 || priceIdx < 0) {
            throw new ParseException("LVN stack needs date,time_uk,price columns");
        }

        List<Snapshot> snaps = new ArrayList<>();
        LocalDateTime curTs = null;
        List<Double> curPrices = new ArrayList<>();
        double curMid = 0;

        DateTimeFormatter dtf = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");
        DateTimeFormatter dtf2 = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");

        for (int li = 1; li < lines.size(); li++) {
            String line = lines.get(li).trim();
            if (line.isEmpty()) {
                continue;
            }
            String[] p = splitCsv(line);
            if (p.length <= Math.max(dateIdx, Math.max(timeIdx, priceIdx))) {
                continue;
            }
            String ds = p[dateIdx].trim().replace("\"", "");
            String ts = p[timeIdx].trim().replace("\"", "");
            LocalDateTime ldt;
            try {
                ldt = LocalDateTime.parse(ds + " " + ts, dtf);
            } catch (DateTimeParseException e1) {
                try {
                    ldt = LocalDateTime.parse(ds + " " + ts, dtf2);
                } catch (DateTimeParseException e2) {
                    continue;
                }
            }
            double px = parseDouble(p[priceIdx]);
            if (px == 0.0) {
                continue;
            }
            double mid = midIdx >= 0 && midIdx < p.length ? parseDouble(p[midIdx]) : 0;

            if (curTs == null) {
                curTs = ldt;
                curPrices = new ArrayList<>();
                curMid = mid;
            }
            if (!ldt.equals(curTs)) {
                if (!curPrices.isEmpty()) {
                    Collections.sort(curPrices);
                    snaps.add(new Snapshot(curTs, curPrices, curMid));
                }
                curTs = ldt;
                curPrices = new ArrayList<>();
                curMid = mid;
            }
            curPrices.add(px);
            if (mid != 0) {
                curMid = mid;
            }
        }
        if (curTs != null && !curPrices.isEmpty()) {
            Collections.sort(curPrices);
            snaps.add(new Snapshot(curTs, curPrices, curMid));
        }
        if (snaps.isEmpty()) {
            throw new ParseException("No stack snapshots in " + path.getFileName());
        }
        return new Stack(path, snaps);
    }

    /** Detect stack file by header (vs IRT chart export / simple LVN CSV). */
    public static boolean looksLikeStack(Path path) throws IOException {
        if (path == null || !Files.isRegularFile(path)) {
            return false;
        }
        try (BufferedReader br = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            String h = br.readLine();
            if (h == null) {
                return false;
            }
            String lc = h.toLowerCase(Locale.ROOT);
            return lc.contains("time_uk") && lc.contains("node_volume");
        }
    }

    private static String[] splitCsv(String line) {
        return line.split(",", -1);
    }

    private static int indexOf(String[] cols, String name) {
        for (int i = 0; i < cols.length; i++) {
            if (cols[i].trim().equalsIgnoreCase(name)) {
                return i;
            }
        }
        return -1;
    }

    private static double parseDouble(String s) {
        if (s == null) {
            return 0;
        }
        s = s.trim().replace("\"", "");
        if (s.isEmpty()) {
            return 0;
        }
        try {
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return 0;
        }
    }
}
