package com.bookmap.orderflow.core;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Load a VAP seed directory produced by {@code research.irt_lvn.build_vap_seed}.
 *
 * <pre>
 * vap_seed_ES/
 *   meta.json          # market, through_uk, tick, window_days_available, …
 *   daily.csv          # day,row_tick,vol
 *   minutes.csv        # minute,row_tick,vol   (partial through-day)
 * </pre>
 */
public final class LvnVapSeedFile {

    public static final class Meta {
        public final String market;
        public final LocalDateTime throughUk;
        public final double tick;
        public final int windowDaysAvailable;
        public final String note;

        public Meta(String market, LocalDateTime throughUk, double tick,
                    int windowDaysAvailable, String note) {
            this.market = market;
            this.throughUk = throughUk;
            this.tick = tick;
            this.windowDaysAvailable = windowDaysAvailable;
            this.note = note == null ? "" : note;
        }
    }

    public static final class Seed {
        public final Path root;
        public final Meta meta;
        /** Whole calendar days → row→vol (immutable copies). */
        public final Map<LocalDate, Map<Integer, Double>> daily;
        /** Minutes on/after first partial day through {@code throughUk}. */
        public final List<MinuteBucket> minutes;

        public Seed(Path root, Meta meta,
                    Map<LocalDate, Map<Integer, Double>> daily,
                    List<MinuteBucket> minutes) {
            this.root = root;
            this.meta = meta;
            this.daily = Collections.unmodifiableMap(new LinkedHashMap<>(daily));
            this.minutes = Collections.unmodifiableList(new ArrayList<>(minutes));
        }
    }

    public static final class MinuteBucket {
        public final LocalDateTime minute;
        public final Map<Integer, Double> vol;

        public MinuteBucket(LocalDateTime minute, Map<Integer, Double> vol) {
            this.minute = minute;
            this.vol = Collections.unmodifiableMap(new HashMap<>(vol));
        }
    }

    public static final class ParseException extends Exception {
        public ParseException(String message) {
            super(message);
        }
    }

    private static final DateTimeFormatter DTF_S =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");
    private static final DateTimeFormatter DTF_M =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");
    private static final Pattern THROUGH =
            Pattern.compile("\"through_uk\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern MARKET =
            Pattern.compile("\"market\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern TICK =
            Pattern.compile("\"tick\"\\s*:\\s*([0-9.]+)");
    private static final Pattern WIN =
            Pattern.compile("\"window_days_available\"\\s*:\\s*(\\d+)");
    private static final Pattern NOTE =
            Pattern.compile("\"note\"\\s*:\\s*\"([^\"]*)\"");

    private LvnVapSeedFile() {}

    public static Seed load(Path path) throws IOException, ParseException {
        if (path == null) {
            throw new ParseException("Seed path is null");
        }
        Path root = resolveRoot(path);
        Path metaPath = root.resolve("meta.json");
        Path dailyPath = root.resolve("daily.csv");
        Path minutesPath = root.resolve("minutes.csv");
        if (!Files.isRegularFile(metaPath)) {
            throw new ParseException("Missing meta.json in " + root);
        }
        Meta meta = parseMeta(metaPath);
        Map<LocalDate, Map<Integer, Double>> daily = Files.isRegularFile(dailyPath)
                ? loadDaily(dailyPath) : Map.of();
        List<MinuteBucket> minutes = Files.isRegularFile(minutesPath)
                ? loadMinutes(minutesPath) : List.of();
        if (daily.isEmpty() && minutes.isEmpty()) {
            throw new ParseException("Seed has no daily.csv / minutes.csv volume: " + root);
        }
        return new Seed(root, meta, daily, minutes);
    }

    /** Accept a seed dir, or a file inside it (meta.json / daily.csv). */
    private static Path resolveRoot(Path path) throws ParseException {
        if (Files.isDirectory(path)) {
            return path;
        }
        if (Files.isRegularFile(path)) {
            Path parent = path.getParent();
            if (parent != null && Files.isRegularFile(parent.resolve("meta.json"))) {
                return parent;
            }
        }
        // vap_seed_ES.zip-less: also accept sibling naming vap_seed_ES_meta.json
        throw new ParseException("Expected seed directory with meta.json: " + path);
    }

    private static Meta parseMeta(Path metaPath) throws IOException, ParseException {
        String text = Files.readString(metaPath, StandardCharsets.UTF_8);
        String market = match(MARKET, text, "ES");
        String throughS = match(THROUGH, text, null);
        if (throughS == null) {
            throw new ParseException("meta.json missing through_uk");
        }
        LocalDateTime through = parseDateTime(throughS);
        if (through == null) {
            throw new ParseException("Bad through_uk: " + throughS);
        }
        double tick = Double.parseDouble(match(TICK, text, "0.25"));
        int win = Integer.parseInt(match(WIN, text, "0"));
        String note = match(NOTE, text, "");
        return new Meta(market.toUpperCase(Locale.ROOT), through, tick, win, note);
    }

    private static String match(Pattern p, String text, String def) {
        Matcher m = p.matcher(text);
        return m.find() ? m.group(1) : def;
    }

    private static Map<LocalDate, Map<Integer, Double>> loadDaily(Path path)
            throws IOException, ParseException {
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        if (lines.isEmpty()) {
            return Map.of();
        }
        String[] cols = splitCsv(lines.get(0));
        int dayIdx = indexOf(cols, "day");
        int rowIdx = indexOf(cols, "row_tick");
        int volIdx = indexOf(cols, "vol");
        if (dayIdx < 0 || rowIdx < 0 || volIdx < 0) {
            throw new ParseException("daily.csv needs day,row_tick,vol");
        }
        Map<LocalDate, Map<Integer, Double>> out = new LinkedHashMap<>();
        for (int i = 1; i < lines.size(); i++) {
            String line = lines.get(i).trim();
            if (line.isEmpty()) {
                continue;
            }
            String[] p = splitCsv(line);
            if (p.length <= Math.max(dayIdx, Math.max(rowIdx, volIdx))) {
                continue;
            }
            LocalDate day = LocalDate.parse(p[dayIdx].trim().replace("\"", ""));
            int row = (int) Math.round(parseDouble(p[rowIdx]));
            double vol = parseDouble(p[volIdx]);
            if (vol <= 0) {
                continue;
            }
            out.computeIfAbsent(day, d -> new HashMap<>())
                    .merge(row, vol, Double::sum);
        }
        return out;
    }

    private static List<MinuteBucket> loadMinutes(Path path)
            throws IOException, ParseException {
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        if (lines.isEmpty()) {
            return List.of();
        }
        String[] cols = splitCsv(lines.get(0));
        int minIdx = indexOf(cols, "minute");
        int rowIdx = indexOf(cols, "row_tick");
        int volIdx = indexOf(cols, "vol");
        if (minIdx < 0 || rowIdx < 0 || volIdx < 0) {
            throw new ParseException("minutes.csv needs minute,row_tick,vol");
        }
        LinkedHashMap<LocalDateTime, Map<Integer, Double>> tmp = new LinkedHashMap<>();
        for (int i = 1; i < lines.size(); i++) {
            String line = lines.get(i).trim();
            if (line.isEmpty()) {
                continue;
            }
            String[] p = splitCsv(line);
            if (p.length <= Math.max(minIdx, Math.max(rowIdx, volIdx))) {
                continue;
            }
            LocalDateTime m = parseDateTime(p[minIdx].trim().replace("\"", ""));
            if (m == null) {
                continue;
            }
            int row = (int) Math.round(parseDouble(p[rowIdx]));
            double vol = parseDouble(p[volIdx]);
            if (vol <= 0) {
                continue;
            }
            tmp.computeIfAbsent(m, k -> new HashMap<>()).merge(row, vol, Double::sum);
        }
        List<MinuteBucket> out = new ArrayList<>(tmp.size());
        for (Map.Entry<LocalDateTime, Map<Integer, Double>> e : tmp.entrySet()) {
            out.add(new MinuteBucket(e.getKey(), e.getValue()));
        }
        return out;
    }

    private static LocalDateTime parseDateTime(String s) {
        if (s == null) {
            return null;
        }
        s = s.trim().replace('T', ' ');
        try {
            return LocalDateTime.parse(s, DTF_S);
        } catch (DateTimeParseException e1) {
            try {
                return LocalDateTime.parse(s, DTF_M);
            } catch (DateTimeParseException e2) {
                try {
                    // date only → midnight
                    return LocalDate.parse(s.substring(0, Math.min(10, s.length())))
                            .atTime(LocalTime.MIDNIGHT);
                } catch (Exception e3) {
                    return null;
                }
            }
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

    /** True if path looks like a seed dir (has meta.json). */
    public static boolean looksLikeSeed(Path path) {
        if (path == null) {
            return false;
        }
        try {
            if (Files.isDirectory(path) && Files.isRegularFile(path.resolve("meta.json"))) {
                return true;
            }
            if (Files.isRegularFile(path) && "meta.json".equalsIgnoreCase(
                    path.getFileName().toString())) {
                return true;
            }
            // scan children named vap_seed_*
            if (Files.isDirectory(path)) {
                try (DirectoryStream<Path> ds = Files.newDirectoryStream(path, "vap_seed_*")) {
                    for (Path p : ds) {
                        if (Files.isDirectory(p)
                                && Files.isRegularFile(p.resolve("meta.json"))) {
                            return true;
                        }
                    }
                }
            }
        } catch (IOException ignored) {
            // fall through
        }
        return false;
    }
}
