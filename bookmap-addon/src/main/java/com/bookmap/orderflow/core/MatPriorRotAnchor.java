package com.bookmap.orderflow.core;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.ZoneId;

import velox.api.layer1.common.Log;

/**
 * Prior-session same-direction rotation averages for MAT075 {@code rot_fresh}.
 *
 * <p>Matches research {@code PriorSessionRotationStats}: when the current UK
 * session has &lt; 2 same-dir completed rotations, fall back to the previous
 * clean session's up/down means. Store:
 * {@code ~/.bookmap-orderflow/mat_prior_rot_avg.txt}
 * format: {@code upAvgPts downAvgPts yyyy-mm-dd}.
 */
public final class MatPriorRotAnchor {

    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final Path STORE = Path.of(
            System.getProperty("user.home"), ".bookmap-orderflow", "mat_prior_rot_avg.txt");

    private MatPriorRotAnchor() {}

    public static Path storePath() {
        return STORE;
    }

    public static final class Prior {
        public final double upAvgPts;
        public final double downAvgPts;

        public Prior(double upAvgPts, double downAvgPts) {
            this.upAvgPts = upAvgPts;
            this.downAvgPts = downAvgPts;
        }

        public static Prior empty() {
            return new Prior(Double.NaN, Double.NaN);
        }

        public boolean usable() {
            return (Double.isFinite(upAvgPts) && upAvgPts > 0)
                    || (Double.isFinite(downAvgPts) && downAvgPts > 0);
        }
    }

    public static Prior load() {
        try {
            if (!Files.isRegularFile(STORE)) {
                return Prior.empty();
            }
            String line = Files.readString(STORE, StandardCharsets.UTF_8).trim();
            if (line.isEmpty()) {
                return Prior.empty();
            }
            String[] parts = line.split("\\s+");
            if (parts.length < 2) {
                return Prior.empty();
            }
            double up = Double.parseDouble(parts[0]);
            double down = Double.parseDouble(parts[1]);
            return new Prior(up, down);
        } catch (Exception e) {
            Log.info("MAT prior-rot: failed to load " + STORE + ": " + e.getMessage());
            return Prior.empty();
        }
    }

    public static void save(double upAvgPts, double downAvgPts) {
        if (!(Double.isFinite(upAvgPts) && upAvgPts > 0)
                && !(Double.isFinite(downAvgPts) && downAvgPts > 0)) {
            return;
        }
        try {
            Path parent = STORE.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            String day = LocalDate.now(UK).toString();
            double up = Double.isFinite(upAvgPts) && upAvgPts > 0 ? upAvgPts : Double.NaN;
            double down = Double.isFinite(downAvgPts) && downAvgPts > 0 ? downAvgPts : Double.NaN;
            String body = String.format(java.util.Locale.US, "%.4f %.4f %s%n", up, down, day);
            Files.writeString(STORE, body, StandardCharsets.UTF_8);
            Log.info("MAT prior-rot: saved up=" + up + " down=" + down + " → " + STORE);
        } catch (IOException e) {
            Log.info("MAT prior-rot: failed to save " + STORE + ": " + e.getMessage());
        }
    }
}
