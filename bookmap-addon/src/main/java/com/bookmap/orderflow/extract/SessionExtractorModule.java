package com.bookmap.orderflow.extract;

import java.io.BufferedOutputStream;
import java.io.BufferedWriter;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.common.Log;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.MarketByOrderDepthDataListener;
import velox.api.layer1.simplified.MultiInstrumentListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Extracts full trade + depth (+ MBO when present) during Bookmap Replay
 * into append-only NDJSON for offline research.
 * Skips MNQ (and other micros if configured). Records NQ + ES only by default.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Session Extractor")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class SessionExtractorModule implements
        CustomModule,
        TradeDataListener,
        DepthDataListener,
        MarketByOrderDepthDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        HistoricalDataListener {

    private static final DateTimeFormatter TS_FMT =
            DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss").withZone(ZoneOffset.UTC);

    @Parameter(name = "Output root directory", step = 0.0, reloadOnChange = true)
    String outputRoot = "C:/Work/Mat/bookmap/data/raw";

    /** Set BEFORE enabling. Changing mid-run reloads and opens a new folder. */
    @Parameter(name = "Session id override (blank=auto)", step = 0.0, reloadOnChange = false)
    String sessionIdOverride = "";

    @Parameter(name = "Skip MNQ / micros")
    Boolean skipMicros = true;

    private String primaryAlias;
    private String currentAlias;
    private Path sessionDir;
    private String sessionId;

    private FileOutputStream tradesFos;
    private FileOutputStream depthFos;
    private FileOutputStream mboFos;
    private BufferedWriter tradesOut;
    private BufferedWriter depthOut;
    private BufferedWriter mboOut;

    private long currentTsNs;
    private long firstTsNs = -1;
    private long lastTsNs = -1;
    private boolean sawMbo;

    private final Map<String, InstrumentState> byAlias = new HashMap<>();
    private final AtomicLong tradeCount = new AtomicLong();
    private final AtomicLong depthCount = new AtomicLong();
    private final AtomicLong mboCount = new AtomicLong();
    private final AtomicLong skippedCount = new AtomicLong();
    private final AtomicLong linesSinceFlush = new AtomicLong();

    private static final class InstrumentState {
        String role = "OTHER";
        String symbol = "";
        String fullName = "";
        double pips = 0.25;
        boolean excluded;
    }

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.primaryAlias = alias;
        this.currentAlias = alias;

        InstrumentState primary = new InstrumentState();
        primary.symbol = info.symbol != null ? info.symbol : "";
        primary.fullName = info.fullName != null ? info.fullName : "";
        primary.pips = info.pips;
        primary.role = classifyRole(primary.symbol, primary.fullName, alias);
        primary.excluded = shouldExclude(primary.symbol, alias, primary.fullName);
        byAlias.put(alias, primary);

        sessionId = (sessionIdOverride != null && !sessionIdOverride.trim().isEmpty())
                ? sessionIdOverride.trim()
                : "session_" + TS_FMT.format(Instant.now());
        try {
            Path root = Paths.get(outputRoot == null || outputRoot.isEmpty()
                    ? "C:/Work/Mat/bookmap/data/raw"
                    : outputRoot);
            Files.createDirectories(root);
            sessionDir = root.resolve(sessionId);
            Files.createDirectories(sessionDir);

            tradesFos = new FileOutputStream(sessionDir.resolve("trades.ndjson").toFile(), true);
            depthFos = new FileOutputStream(sessionDir.resolve("depth.ndjson").toFile(), true);
            mboFos = new FileOutputStream(sessionDir.resolve("mbo.ndjson").toFile(), true);
            tradesOut = new BufferedWriter(new OutputStreamWriter(
                    new BufferedOutputStream(tradesFos, 1 << 20), StandardCharsets.UTF_8));
            depthOut = new BufferedWriter(new OutputStreamWriter(
                    new BufferedOutputStream(depthFos, 1 << 20), StandardCharsets.UTF_8));
            mboOut = new BufferedWriter(new OutputStreamWriter(
                    new BufferedOutputStream(mboFos, 1 << 20), StandardCharsets.UTF_8));

            writeMetaPartial();
            Log.info("Session Extractor writing to " + sessionDir.toAbsolutePath()
                    + " skipMicros=" + (skipMicros == null || skipMicros));
        } catch (IOException e) {
            Log.error("Session Extractor failed to open output: " + e.getMessage());
            throw new RuntimeException(e);
        }
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : (info.symbol != null ? info.symbol : "unknown");
        InstrumentState state = byAlias.computeIfAbsent(alias, a -> new InstrumentState());
        state.symbol = info.symbol != null ? info.symbol : state.symbol;
        state.fullName = info.fullName != null ? info.fullName : state.fullName;
        state.pips = info.pips > 0 ? info.pips : state.pips;
        state.role = classifyRole(state.symbol, state.fullName, alias);
        state.excluded = shouldExclude(state.symbol, alias, state.fullName);
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        InstrumentState state = byAlias.computeIfAbsent(alias, a -> new InstrumentState());
        if (state.symbol.isEmpty()) {
            state.excluded = shouldExclude("", alias, "");
        }
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        currentTsNs = nanoseconds;
        if (firstTsNs < 0) {
            firstTsNs = nanoseconds;
        }
        lastTsNs = nanoseconds;
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        InstrumentState st = stateForCurrent();
        if (st.excluded) {
            skippedCount.incrementAndGet();
            return;
        }
        if (size <= 0) {
            return;
        }
        String aggressorId = tradeInfo.aggressorOrderId != null ? tradeInfo.aggressorOrderId : "";
        boolean buy = tradeInfo.isBidAggressor;
        String line = jsonObject(
                "ts_ns", Long.toString(currentTsNs),
                "alias", jsonString(currentAlias),
                "role", jsonString(st.role),
                "symbol", jsonString(st.symbol),
                "price", Double.toString(price),
                "size", Integer.toString(size),
                "is_buy_aggressor", buy ? "true" : "false",
                "aggressor_order_id", jsonString(aggressorId),
                "is_exec_start", tradeInfo.isExecutionStart ? "true" : "false",
                "is_exec_end", tradeInfo.isExecutionEnd ? "true" : "false"
        );
        writeLine(tradesOut, line);
        tradeCount.incrementAndGet();
        maybeFlush();
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        InstrumentState st = stateForCurrent();
        if (st.excluded) {
            skippedCount.incrementAndGet();
            return;
        }
        String line = jsonObject(
                "ts_ns", Long.toString(currentTsNs),
                "alias", jsonString(currentAlias),
                "role", jsonString(st.role),
                "symbol", jsonString(st.symbol),
                "is_bid", isBid ? "true" : "false",
                "price_ticks", Integer.toString(price),
                "size", Integer.toString(size)
        );
        writeLine(depthOut, line);
        depthCount.incrementAndGet();
        maybeFlush();
    }

    @Override
    public void send(String orderId, boolean isBid, int price, int size) {
        writeMbo("send", orderId, isBid, price, size);
    }

    @Override
    public void replace(String orderId, int price, int size) {
        writeMbo("replace", orderId, null, price, size);
    }

    @Override
    public void cancel(String orderId) {
        writeMbo("cancel", orderId, null, 0, 0);
    }

    private void writeMbo(String op, String orderId, Boolean isBid, int price, int size) {
        InstrumentState st = stateForCurrent();
        if (st.excluded) {
            skippedCount.incrementAndGet();
            return;
        }
        sawMbo = true;
        StringBuilder sb = new StringBuilder(160);
        sb.append("{\"ts_ns\":").append(currentTsNs)
                .append(",\"alias\":").append(jsonString(currentAlias))
                .append(",\"role\":").append(jsonString(st.role))
                .append(",\"symbol\":").append(jsonString(st.symbol))
                .append(",\"op\":").append(jsonString(op))
                .append(",\"order_id\":").append(jsonString(orderId == null ? "" : orderId));
        if (isBid != null) {
            sb.append(",\"is_bid\":").append(isBid ? "true" : "false");
        }
        sb.append(",\"price_ticks\":").append(price)
                .append(",\"size\":").append(size)
                .append('}');
        writeLine(mboOut, sb.toString());
        mboCount.incrementAndGet();
        maybeFlush();
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_1_SECOND;
    }

    @Override
    public void onInterval() {
        try {
            flushAll(false);
            writeMetaPartial();
        } catch (IOException e) {
            Log.warn("Session Extractor flush error: " + e.getMessage());
        }
    }

    @Override
    public void stop() {
        try {
            flushAll(true);
            writeMetaFinal();
            closeQuietly(tradesOut, tradesFos);
            closeQuietly(depthOut, depthFos);
            closeQuietly(mboOut, mboFos);
            tradesOut = null;
            depthOut = null;
            mboOut = null;
            Log.info(String.format(
                    "Session Extractor stopped. trades=%d depth=%d mbo=%d skipped=%d dir=%s",
                    tradeCount.get(), depthCount.get(), mboCount.get(), skippedCount.get(),
                    sessionDir != null ? sessionDir.toAbsolutePath() : "?"));
        } catch (IOException e) {
            Log.error("Session Extractor stop error: " + e.getMessage());
        }
    }

    private InstrumentState stateForCurrent() {
        return byAlias.computeIfAbsent(currentAlias, a -> {
            InstrumentState s = new InstrumentState();
            s.excluded = shouldExclude("", a, "");
            return s;
        });
    }

    private boolean shouldExclude(String symbol, String alias, String fullName) {
        if (skipMicros != null && !skipMicros) {
            return false;
        }
        String sym = symbol != null ? symbol.toUpperCase(Locale.ROOT) : "";
        String al = alias != null ? alias.toUpperCase(Locale.ROOT) : "";
        String full = fullName != null ? fullName.toUpperCase(Locale.ROOT) : "";
        // Micro E-mini Nasdaq
        if (sym.startsWith("MNQ") || al.contains("MNQ") || full.contains("MICRO E-MINI NASDAQ")
                || full.contains("MICRO NASDAQ")) {
            return true;
        }
        return false;
    }

    private void maybeFlush() {
        if (linesSinceFlush.incrementAndGet() >= 2000) {
            linesSinceFlush.set(0);
            try {
                flushAll(false);
            } catch (IOException e) {
                Log.warn("Session Extractor flush: " + e.getMessage());
            }
        }
    }

    private void flushAll(boolean forceToDisk) throws IOException {
        if (tradesOut != null) {
            tradesOut.flush();
        }
        if (depthOut != null) {
            depthOut.flush();
        }
        if (mboOut != null) {
            mboOut.flush();
        }
        if (forceToDisk) {
            if (tradesFos != null) {
                tradesFos.getFD().sync();
            }
            if (depthFos != null) {
                depthFos.getFD().sync();
            }
            if (mboFos != null) {
                mboFos.getFD().sync();
            }
        }
    }

    private void writeMetaPartial() throws IOException {
        writeMeta(false);
    }

    private void writeMetaFinal() throws IOException {
        writeMeta(true);
    }

    private void writeMeta(boolean finished) throws IOException {
        if (sessionDir == null) {
            return;
        }
        StringBuilder instruments = new StringBuilder("[");
        boolean first = true;
        for (Map.Entry<String, InstrumentState> e : byAlias.entrySet()) {
            if (!first) {
                instruments.append(',');
            }
            first = false;
            InstrumentState s = e.getValue();
            instruments.append('{')
                    .append("\"alias\":").append(jsonString(e.getKey()))
                    .append(",\"role\":").append(jsonString(s.role))
                    .append(",\"symbol\":").append(jsonString(s.symbol))
                    .append(",\"full_name\":").append(jsonString(s.fullName))
                    .append(",\"excluded\":").append(s.excluded ? "true" : "false")
                    .append(",\"pips\":").append(s.pips)
                    .append('}');
        }
        instruments.append(']');

        String json = "{\n"
                + "  \"session_id\": " + jsonString(sessionId) + ",\n"
                + "  \"primary_alias\": " + jsonString(primaryAlias) + ",\n"
                + "  \"output_dir\": " + jsonString(sessionDir.toAbsolutePath().toString()) + ",\n"
                + "  \"finished\": " + (finished ? "true" : "false") + ",\n"
                + "  \"first_ts_ns\": " + firstTsNs + ",\n"
                + "  \"last_ts_ns\": " + lastTsNs + ",\n"
                + "  \"trade_count\": " + tradeCount.get() + ",\n"
                + "  \"depth_count\": " + depthCount.get() + ",\n"
                + "  \"mbo_count\": " + mboCount.get() + ",\n"
                + "  \"skipped_count\": " + skippedCount.get() + ",\n"
                + "  \"mbo_seen\": " + (sawMbo ? "true" : "false") + ",\n"
                + "  \"skip_micros\": " + (skipMicros == null || skipMicros ? "true" : "false") + ",\n"
                + "  \"instruments\": " + instruments + "\n"
                + "}\n";
        Files.write(sessionDir.resolve("meta.json"), json.getBytes(StandardCharsets.UTF_8));
    }

    private static void writeLine(BufferedWriter w, String line) {
        if (w == null) {
            return;
        }
        try {
            w.write(line);
            w.newLine();
        } catch (IOException e) {
            Log.warn("Session Extractor write failed: " + e.getMessage());
        }
    }

    private static void closeQuietly(BufferedWriter w, FileOutputStream fos) {
        try {
            if (w != null) {
                w.close();
            }
        } catch (IOException ignored) {
            // ignore
        }
        try {
            if (fos != null) {
                fos.close();
            }
        } catch (IOException ignored) {
            // ignore
        }
    }

    private static String jsonObject(String... kv) {
        StringBuilder sb = new StringBuilder(128);
        sb.append('{');
        for (int i = 0; i + 1 < kv.length; i += 2) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append('"').append(kv[i]).append("\":");
            sb.append(kv[i + 1]);
        }
        sb.append('}');
        return sb.toString();
    }

    private static String jsonString(String s) {
        if (s == null) {
            return "\"\"";
        }
        String escaped = s
                .replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r");
        return "\"" + escaped + "\"";
    }

    static String classifyRole(String symbol, String fullName, String alias) {
        String sym = symbol != null ? symbol.toUpperCase(Locale.ROOT) : "";
        String full = fullName != null ? fullName.toUpperCase(Locale.ROOT) : "";
        String al = alias != null ? alias.toUpperCase(Locale.ROOT) : "";
        String combined = sym + " " + full + " " + al;
        // Micros classified but normally excluded from write path
        if (sym.startsWith("MNQ") || al.contains("MNQ") || combined.contains("MICRO E-MINI NASDAQ")) {
            return "NQ";
        }
        if (sym.startsWith("MES") || al.contains("MES") || combined.contains("MICRO E-MINI S&P")) {
            return "ES";
        }
        if (combined.contains("NASDAQ") || sym.startsWith("NQ") || al.contains(":NQ") || al.startsWith("NQ")) {
            return "NQ";
        }
        if (combined.contains("S&P") || combined.contains("SP500") || sym.startsWith("ES")
                || al.contains(":ES") || al.startsWith("ES")) {
            return "ES";
        }
        return "OTHER";
    }
}
