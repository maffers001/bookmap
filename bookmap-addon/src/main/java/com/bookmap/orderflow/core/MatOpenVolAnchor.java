package com.bookmap.orderflow.core;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;

import velox.api.layer1.common.Log;

/**
 * Open-period vol calibration for MAT / CFADE brackets.
 *
 * <p>During the first {@code openPeriodMin} minutes after the UK session open
 * (default 14:30), live median 1-min range is often too quiet. Prefer a
 * <b>prior-session</b> median persisted from the previous RTH window; if none
 * is available, fall back to a fixed open bracket size.
 *
 * <p>Shared store under {@code ~/.bookmap-orderflow/mat_prior_vol_median.txt}
 * so MAT and CFADE reuse the same prior NQ session median.
 */
public final class MatOpenVolAnchor {

    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final Path STORE = Path.of(
            System.getProperty("user.home"), ".bookmap-orderflow", "mat_prior_vol_median.txt");

    private MatOpenVolAnchor() {}

    public static Path storePath() {
        return STORE;
    }

    /** Minutes since UK session open (mod), or -1 if before open today. */
    public static int minutesSinceSessionOpen(long tsNs, int openUkMinOfDay) {
        long sec = tsNs / 1_000_000_000L;
        long nano = tsNs % 1_000_000_000L;
        ZonedDateTime zdt = Instant.ofEpochSecond(sec, nano).atZone(UK);
        int mod = zdt.getHour() * 60 + zdt.getMinute();
        if (mod < openUkMinOfDay) {
            return -1;
        }
        return mod - openUkMinOfDay;
    }

    public static boolean inOpenPeriod(long tsNs, int openUkMinOfDay, int openPeriodMin) {
        int m = minutesSinceSessionOpen(tsNs, openUkMinOfDay);
        return m >= 0 && m < Math.max(1, openPeriodMin);
    }

    /**
     * Load prior median minute range (chart pts). {@code NaN} if missing/invalid.
     */
    public static double loadPriorMedian() {
        try {
            if (!Files.isRegularFile(STORE)) {
                return Double.NaN;
            }
            String line = Files.readString(STORE, StandardCharsets.UTF_8).trim();
            if (line.isEmpty()) {
                return Double.NaN;
            }
            // format: medianPts [nBars] [yyyy-mm-dd]
            String[] parts = line.split("\\s+");
            double v = Double.parseDouble(parts[0]);
            if (!Double.isFinite(v) || v <= 0) {
                return Double.NaN;
            }
            return v;
        } catch (Exception e) {
            Log.info("MAT open-vol: failed to load prior from " + STORE + ": " + e.getMessage());
            return Double.NaN;
        }
    }

    /** Persist today's session median for tomorrow's open. */
    public static void savePriorMedian(double medianPts, int nBars) {
        if (!Double.isFinite(medianPts) || medianPts <= 0) {
            return;
        }
        try {
            Path parent = STORE.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            String day = java.time.LocalDate.now(UK).toString();
            String body = String.format(java.util.Locale.US, "%.4f %d %s%n", medianPts, nBars, day);
            Files.writeString(STORE, body, StandardCharsets.UTF_8);
        } catch (IOException e) {
            Log.info("MAT open-vol: failed to save prior to " + STORE + ": " + e.getMessage());
        }
    }
}
