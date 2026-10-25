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
import java.util.List;
import java.util.Locale;

/**
 * Parse pre-open LVN2/LVN3 (and optional HVN1/2/3) from IRT chart exports.
 *
 * <p>Accepts:
 * <ul>
 *   <li>Simple CSV: {@code date,lvn2,lvn3} (optional header; HVNs blank).</li>
 *   <li>Full Investor/RT chart export — resolves LVN2/LVN3/HVN1/2/3 by first
 *       occurrence, like research {@code load_irt.resolve_columns}.</li>
 * </ul>
 */
public final class LvnLevelFile {

    public static final ZoneId UK = ZoneId.of("Europe/London");
    /** Research align warn threshold (~25 pt MAD → basis check). */
    public static final double BASIS_WARN_PTS = 25.0;

    public static final class Levels {
        public final LocalDate date;
        public final double lvn2;
        public final double lvn3;
        public final double hvn1;
        public final double hvn2;
        public final double hvn3;
        public final Path source;
        public final String format;
        public final String note;

        public Levels(LocalDate date, double lvn2, double lvn3, Path source, String format, String note) {
            this(date, lvn2, lvn3, 0, 0, 0, source, format, note);
        }

        public Levels(LocalDate date, double lvn2, double lvn3,
                      double hvn1, double hvn2, double hvn3,
                      Path source, String format, String note) {
            this.date = date;
            this.lvn2 = lvn2;
            this.lvn3 = lvn3;
            this.hvn1 = hvn1;
            this.hvn2 = hvn2;
            this.hvn3 = hvn3;
            this.source = source;
            this.format = format;
            this.note = note == null ? "" : note;
        }

        public List<Double> prices() {
            List<Double> out = new ArrayList<>(2);
            if (lvn2 != 0.0) {
                out.add(lvn2);
            }
            if (lvn3 != 0.0 && Math.abs(lvn3 - lvn2) > 1e-9) {
                out.add(lvn3);
            }
            return out;
        }

        public boolean hasAny() {
            return lvn2 != 0.0 || lvn3 != 0.0;
        }

        public boolean hasAnyHvn() {
            return hvn1 != 0.0 || hvn2 != 0.0 || hvn3 != 0.0;
        }
    }

    public static final class ParseException extends Exception {
        public ParseException(String message) {
            super(message);
        }
    }

    private LvnLevelFile() {}

    /**
     * Load LVNs for {@code sessionDate}. If the file is a multi-day IRT export,
     * takes the last non-zero LVN2/LVN3 row on that date at/before 14:30 UK.
     */
    public static Levels load(Path path, LocalDate sessionDate) throws IOException, ParseException {
        if (path == null || !Files.isRegularFile(path)) {
            throw new ParseException("LVN file not found: " + path);
        }
        List<String> lines = readLines(path);
        if (lines.isEmpty()) {
            throw new ParseException("Empty LVN file: " + path);
        }
        String header = lines.get(0);
        String[] cols = splitCsv(header);
        int dateIdx = indexOf(cols, "Date", 1);
        int lvn2Idx = indexOf(cols, "LVN2", 1);
        int lvn3Idx = indexOf(cols, "LVN3", 1);
        int hvn1Idx = indexOf(cols, "HVN1", 1);
        int hvn2Idx = indexOf(cols, "HVN2", 1);
        int hvn3Idx = indexOf(cols, "HVN3", 1);
        // Simple format may use lowercase / alternate names
        if (dateIdx < 0) {
            dateIdx = indexOfIgnoreCase(cols, "date");
        }
        if (lvn2Idx < 0) {
            lvn2Idx = indexOfIgnoreCase(cols, "lvn2");
        }
        if (lvn3Idx < 0) {
            lvn3Idx = indexOfIgnoreCase(cols, "lvn3");
        }
        if (hvn1Idx < 0) {
            hvn1Idx = indexOfIgnoreCase(cols, "hvn1");
        }
        if (hvn2Idx < 0) {
            hvn2Idx = indexOfIgnoreCase(cols, "hvn2");
        }
        if (hvn3Idx < 0) {
            hvn3Idx = indexOfIgnoreCase(cols, "hvn3");
        }

        boolean hasHeader = dateIdx >= 0 && lvn2Idx >= 0 && lvn3Idx >= 0;
        if (hasHeader && looksLikeIrtExport(cols)) {
            return loadIrtExport(path, lines, cols, dateIdx, lvn2Idx, lvn3Idx,
                    hvn1Idx, hvn2Idx, hvn3Idx, sessionDate);
        }
        if (hasHeader) {
            return loadSimpleWithHeader(path, lines, dateIdx, lvn2Idx, lvn3Idx,
                    hvn1Idx, hvn2Idx, hvn3Idx, sessionDate);
        }
        // No header: try first data row as date,lvn2,lvn3
        return loadSimpleNoHeader(path, lines, sessionDate);
    }

    /** Optional mid check — returns warning string or null if OK. */
    public static String basisWarning(Levels levels, double midPts) {
        if (levels == null || !levels.hasAny() || !(midPts > 0)) {
            return null;
        }
        double nearest = Double.POSITIVE_INFINITY;
        for (double px : levels.prices()) {
            nearest = Math.min(nearest, Math.abs(px - midPts));
        }
        // LVNs can be far from mid (tens of pts); warn only if ALL are absurdly far
        // OR every level is hundreds of pts away (wrong contract / continuous basis).
        double minDist = nearest;
        if (minDist >= BASIS_WARN_PTS * 10) {
            return String.format(Locale.US,
                    "LVN prices look misaligned vs mid %.2f (nearest %.0f pts away) — check contract/basis",
                    midPts, minDist);
        }
        return null;
    }

    public static String staleWarning(Levels levels, LocalDate todayUk) {
        if (levels == null || todayUk == null) {
            return null;
        }
        if (!levels.date.equals(todayUk)) {
            return "LVN file date " + levels.date + " ≠ today " + todayUk + " (stale)";
        }
        return null;
    }

    // ------------------------------------------------------------------ parsers

    private static Levels loadIrtExport(
            Path path, List<String> lines, String[] cols,
            int dateIdx, int lvn2Idx, int lvn3Idx,
            int hvn1Idx, int hvn2Idx, int hvn3Idx,
            LocalDate sessionDate) throws ParseException {
        LocalDateTime cutoff = LocalDateTime.of(sessionDate, LocalTime.of(14, 30));
        double bestLvn2 = 0;
        double bestLvn3 = 0;
        double bestHvn1 = 0;
        double bestHvn2 = 0;
        double bestHvn3 = 0;
        LocalDateTime bestTs = null;
        int matched = 0;
        int needMax = Math.max(dateIdx, Math.max(lvn2Idx, lvn3Idx));
        for (int i = 1; i < lines.size(); i++) {
            String[] parts = splitCsv(lines.get(i));
            if (parts.length <= needMax) {
                continue;
            }
            LocalDateTime ts = parseIrtDateTime(parts[dateIdx]);
            if (ts == null) {
                continue;
            }
            if (!ts.toLocalDate().equals(sessionDate)) {
                continue;
            }
            if (ts.isAfter(cutoff)) {
                continue;
            }
            double v2 = parseDouble(parts[lvn2Idx]);
            double v3 = parseDouble(parts[lvn3Idx]);
            if (v2 == 0.0 && v3 == 0.0) {
                continue;
            }
            matched++;
            if (bestTs == null || !ts.isBefore(bestTs)) {
                bestTs = ts;
                bestLvn2 = v2;
                bestLvn3 = v3;
                bestHvn1 = colVal(parts, hvn1Idx);
                bestHvn2 = colVal(parts, hvn2Idx);
                bestHvn3 = colVal(parts, hvn3Idx);
            }
        }
        if (matched == 0 || (bestLvn2 == 0.0 && bestLvn3 == 0.0)) {
            throw new ParseException(
                    "No non-zero LVN2/LVN3 on " + sessionDate + " at/before 14:30 in " + path.getFileName());
        }
        return new Levels(sessionDate, bestLvn2, bestLvn3, bestHvn1, bestHvn2, bestHvn3,
                path, "irt_export", "as-of " + bestTs + " UK; rows=" + matched);
    }

    private static Levels loadSimpleWithHeader(
            Path path, List<String> lines, int dateIdx, int lvn2Idx, int lvn3Idx,
            int hvn1Idx, int hvn2Idx, int hvn3Idx, LocalDate sessionDate)
            throws ParseException {
        Levels last = null;
        int needMax = Math.max(dateIdx, Math.max(lvn2Idx, lvn3Idx));
        for (int i = 1; i < lines.size(); i++) {
            String[] parts = splitCsv(lines.get(i));
            if (parts.length <= needMax) {
                continue;
            }
            LocalDate d = parseDateOnly(parts[dateIdx]);
            if (d == null) {
                continue;
            }
            if (sessionDate != null && !d.equals(sessionDate)) {
                continue;
            }
            double v2 = parseDouble(parts[lvn2Idx]);
            double v3 = parseDouble(parts[lvn3Idx]);
            if (v2 == 0.0 && v3 == 0.0) {
                continue;
            }
            last = new Levels(d, v2, v3,
                    colVal(parts, hvn1Idx), colVal(parts, hvn2Idx), colVal(parts, hvn3Idx),
                    path, "simple_csv", "row=" + i);
        }
        if (last == null) {
            // Fall back: take last row regardless of date (user may load a one-row file)
            for (int i = lines.size() - 1; i >= 1; i--) {
                String[] parts = splitCsv(lines.get(i));
                if (parts.length <= needMax) {
                    continue;
                }
                LocalDate d = parseDateOnly(parts[dateIdx]);
                double v2 = parseDouble(parts[lvn2Idx]);
                double v3 = parseDouble(parts[lvn3Idx]);
                if (v2 == 0.0 && v3 == 0.0) {
                    continue;
                }
                if (d == null) {
                    d = sessionDate != null ? sessionDate : LocalDate.now(UK);
                }
                last = new Levels(d, v2, v3,
                        colVal(parts, hvn1Idx), colVal(parts, hvn2Idx), colVal(parts, hvn3Idx),
                        path, "simple_csv", "last-row fallback");
                break;
            }
        }
        if (last == null) {
            throw new ParseException("No LVN rows in " + path.getFileName());
        }
        return last;
    }

    private static double colVal(String[] parts, int idx) {
        if (idx < 0 || idx >= parts.length) {
            return 0.0;
        }
        return parseDouble(parts[idx]);
    }

    private static Levels loadSimpleNoHeader(Path path, List<String> lines, LocalDate sessionDate)
            throws ParseException {
        for (String line : lines) {
            String[] parts = splitCsv(line);
            if (parts.length < 3) {
                continue;
            }
            LocalDate d = parseDateOnly(parts[0]);
            double v2 = parseDouble(parts[1]);
            double v3 = parseDouble(parts[2]);
            if (v2 == 0.0 && v3 == 0.0) {
                continue;
            }
            if (d == null) {
                d = sessionDate != null ? sessionDate : LocalDate.now(UK);
            }
            return new Levels(d, v2, v3, path, "simple_no_header", "");
        }
        throw new ParseException("Expected date,lvn2,lvn3 rows in " + path.getFileName());
    }

    // ------------------------------------------------------------------ helpers

    private static boolean looksLikeIrtExport(String[] cols) {
        int dates = 0;
        int closes = 0;
        for (String c : cols) {
            if ("Date".equalsIgnoreCase(c.trim())) {
                dates++;
            }
            if ("Close".equalsIgnoreCase(c.trim())) {
                closes++;
            }
        }
        return dates >= 1 && (closes >= 1 || cols.length > 8);
    }

    private static List<String> readLines(Path path) throws IOException {
        List<String> out = new ArrayList<>();
        try (BufferedReader br = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (!line.isEmpty()) {
                    out.add(line);
                }
            }
        }
        return out;
    }

    private static String[] splitCsv(String line) {
        // IRT exports are simple comma-separated; strip quotes.
        String[] raw = line.split(",", -1);
        for (int i = 0; i < raw.length; i++) {
            String s = raw[i].trim();
            if (s.length() >= 2 && s.startsWith("\"") && s.endsWith("\"")) {
                s = s.substring(1, s.length() - 1);
            }
            raw[i] = s;
        }
        return raw;
    }

    private static int indexOf(String[] cols, String name, int n) {
        int found = 0;
        for (int i = 0; i < cols.length; i++) {
            if (name.equals(cols[i].trim())) {
                found++;
                if (found == n) {
                    return i;
                }
            }
        }
        return -1;
    }

    private static int indexOfIgnoreCase(String[] cols, String name) {
        for (int i = 0; i < cols.length; i++) {
            if (name.equalsIgnoreCase(cols[i].trim())) {
                return i;
            }
        }
        return -1;
    }

    private static double parseDouble(String s) {
        if (s == null) {
            return 0.0;
        }
        s = s.trim();
        if (s.isEmpty()) {
            return 0.0;
        }
        try {
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return 0.0;
        }
    }

    private static LocalDate parseDateOnly(String s) {
        if (s == null) {
            return null;
        }
        s = s.trim().replace("\"", "");
        if (s.length() >= 10) {
            s = s.substring(0, 10);
        }
        try {
            return LocalDate.parse(s, DateTimeFormatter.ISO_LOCAL_DATE);
        } catch (DateTimeParseException e) {
            return null;
        }
    }

    private static LocalDateTime parseIrtDateTime(String s) {
        if (s == null) {
            return null;
        }
        s = s.trim().replace("\"", "");
        String[] fmts = {
                "yyyy-MM-dd HH:mm:ss",
                "yyyy-MM-dd HH:mm",
                "yyyy-MM-dd"
        };
        for (String fmt : fmts) {
            try {
                if (fmt.equals("yyyy-MM-dd") && s.length() >= 10) {
                    return LocalDate.parse(s.substring(0, 10)).atStartOfDay();
                }
                return LocalDateTime.parse(s, DateTimeFormatter.ofPattern(fmt));
            } catch (DateTimeParseException ignored) {
                // try next
            }
        }
        return null;
    }
}
