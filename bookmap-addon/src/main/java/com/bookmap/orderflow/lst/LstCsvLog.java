package com.bookmap.orderflow.lst;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.Locale;

import velox.api.layer1.common.Log;

/** Append-only CSV logger for LST signal / trade parity checks. */
final class LstCsvLog implements AutoCloseable {

    private final String name;
    private BufferedWriter writer;
    private boolean headerWritten;

    LstCsvLog(String name, String path) {
        this.name = name;
        open(path);
    }

    private void open(String path) {
        if (path == null || path.trim().isEmpty()) {
            return;
        }
        try {
            Path p = Paths.get(path.trim());
            if (p.getParent() != null) {
                Files.createDirectories(p.getParent());
            }
            boolean exists = Files.exists(p) && Files.size(p) > 0;
            writer = Files.newBufferedWriter(p, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
            headerWritten = exists;
            Log.info(name + " log → " + p.toAbsolutePath());
        } catch (IOException e) {
            Log.warn(name + " cannot open log: " + e.getMessage());
            writer = null;
        }
    }

    void writeHeaderOnce(String header) {
        if (writer == null || headerWritten) {
            return;
        }
        try {
            writer.write(header);
            writer.newLine();
            writer.flush();
            headerWritten = true;
        } catch (IOException e) {
            Log.warn(name + " header write failed: " + e.getMessage());
        }
    }

    void writeRow(String row) {
        if (writer == null) {
            return;
        }
        try {
            writer.write(row);
            writer.newLine();
            writer.flush();
        } catch (IOException e) {
            Log.warn(name + " row write failed: " + e.getMessage());
        }
    }

    static String f(double v) {
        return Double.isFinite(v) ? String.format(Locale.US, "%.4f", v) : "";
    }

    @Override
    public void close() {
        if (writer != null) {
            try {
                writer.close();
            } catch (IOException ignored) {
            }
            writer = null;
        }
    }
}
