package com.bookmap.orderflow.crossmarket;

import java.awt.Color;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

import com.bookmap.orderflow.core.FlowBucket;
import com.bookmap.orderflow.core.RollingStats;

import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyIndicator.GraphType;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.AxisGroup;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.MultiInstrumentListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Normalized ES/NQ aggressive-flow agreement for correlated order-flow entries.
 * Attach on the NQ chart with ES also open in Bookmap.
 *
 * States (panel code): 0 NEUTRAL, 1 AGREE_BULL, -1 AGREE_BEAR, 2 DIVERGE
 * Lead: +1 ES led, -1 NQ led, 0 none
 */
@Layer1SimpleAttachable
@Layer1StrategyName("ES/NQ Flow Confirm")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class EsNqConfirmModule implements
        CustomModule,
        TradeDataListener,
        IntervalListener,
        MultiInstrumentListener,
        HistoricalDataListener {

    @Parameter(name = "Z-score window", step = 1.0)
    Double zWindow = 40.0;

    @Parameter(name = "Agree z threshold", step = 0.1)
    Double agreeZ = 0.75;

    private String primaryAlias;
    private String currentAlias;
    private final Map<String, InstrumentState> byAlias = new HashMap<>();
    private final Map<String, String> roleByAlias = new HashMap<>();

    private Indicator confirmCodeIndicator;
    private Indicator nqDeltaZIndicator;
    private Indicator esDeltaZIndicator;
    private Indicator leadIndicator;

    private long lastEsFlipNs;
    private long lastNqFlipNs;
    private int lastEsSign;
    private int lastNqSign;
    private long nowBucket;

    private static final class InstrumentState {
        final FlowBucket bucket = new FlowBucket();
        final RollingStats deltaStats;
        String role = "OTHER"; // NQ, ES, OTHER
        double lastDeltaZ;
        int lastSign;

        InstrumentState(int window) {
            this.deltaStats = new RollingStats(window);
        }
    }

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.primaryAlias = alias;
        this.currentAlias = alias;
        int window = Math.max(10, zWindow == null ? 40 : zWindow.intValue());

        InstrumentState primary = new InstrumentState(window);
        primary.role = classifySymbol(info);
        byAlias.put(alias, primary);
        roleByAlias.put(alias, primary.role);

        confirmCodeIndicator = api.registerIndicator("XN Confirm", GraphType.BOTTOM);
        nqDeltaZIndicator = api.registerIndicator("XN NQ Delta Z", GraphType.BOTTOM);
        esDeltaZIndicator = api.registerIndicator("XN ES Delta Z", GraphType.BOTTOM);
        leadIndicator = api.registerIndicator("XN Lead (ES+ / NQ-)", GraphType.BOTTOM);

        confirmCodeIndicator.setColor(new Color(255, 220, 80));
        nqDeltaZIndicator.setColor(new Color(0, 180, 255));
        esDeltaZIndicator.setColor(new Color(255, 140, 0));
        leadIndicator.setColor(new Color(180, 180, 180));

        AxisGroup group = new AxisGroup();
        group.add(nqDeltaZIndicator);
        group.add(esDeltaZIndicator);
        group.add(confirmCodeIndicator);
        group.add(leadIndicator);
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        // Alias is current instrument set just before this in multi-instrument mode.
        String alias = currentAlias != null ? currentAlias : info.symbol;
        int window = Math.max(10, zWindow == null ? 40 : zWindow.intValue());
        InstrumentState state = byAlias.computeIfAbsent(alias, a -> new InstrumentState(window));
        state.role = classifySymbol(info);
        roleByAlias.put(alias, state.role);
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        int window = Math.max(10, zWindow == null ? 40 : zWindow.intValue());
        byAlias.computeIfAbsent(alias, a -> new InstrumentState(window));
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        InstrumentState state = byAlias.get(currentAlias);
        if (state == null) {
            int window = Math.max(10, zWindow == null ? 40 : zWindow.intValue());
            state = new InstrumentState(window);
            byAlias.put(currentAlias, state);
        }
        state.bucket.onTrade(tradeInfo.isBidAggressor, size, tradeInfo.aggressorOrderId);
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_250_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        nowBucket++;
        InstrumentState nq = findByRole("NQ");
        InstrumentState es = findByRole("ES");

        double nqZ = updateAndZ(nq);
        double esZ = updateAndZ(es);

        nqDeltaZIndicator.addPoint(nqZ);
        esDeltaZIndicator.addPoint(es == null ? 0.0 : esZ);

        double threshold = agreeZ == null ? 0.75 : agreeZ;
        int nqSign = sign(nqZ, threshold);
        int esSign = sign(esZ, threshold);

        if (nq != null && nqSign != 0 && nqSign != lastNqSign) {
            lastNqFlipNs = nowBucket;
            lastNqSign = nqSign;
        }
        if (es != null && esSign != 0 && esSign != lastEsSign) {
            lastEsFlipNs = nowBucket;
            lastEsSign = esSign;
        }

        double confirm;
        if (nqSign != 0 && esSign != 0 && nqSign == esSign) {
            confirm = nqSign; // +1 bull, -1 bear
        } else if (nqSign != 0 && esSign != 0 && nqSign != esSign) {
            confirm = 2; // diverge
        } else {
            confirm = 0;
        }
        confirmCodeIndicator.addPoint(confirm);

        double lead = 0;
        // Lead within ~2s at 250ms buckets => 8 buckets
        if (lastEsFlipNs > 0 && lastNqFlipNs > 0
                && Math.abs(lastEsFlipNs - lastNqFlipNs) <= 8
                && lastEsSign != 0 && lastNqSign != 0
                && lastEsSign == lastNqSign) {
            lead = lastEsFlipNs < lastNqFlipNs ? 1 : -1;
        }
        leadIndicator.addPoint(lead);

        if (nq != null) {
            nq.bucket.reset();
        }
        if (es != null) {
            es.bucket.reset();
        }
        // Reset any other tracked aliases too
        for (InstrumentState state : byAlias.values()) {
            if (state != nq && state != es) {
                state.bucket.reset();
            }
        }
    }

    private double updateAndZ(InstrumentState state) {
        if (state == null) {
            return 0;
        }
        double delta = state.bucket.getVolumeDelta();
        state.deltaStats.add(delta);
        state.lastDeltaZ = state.deltaStats.zScore(delta);
        return state.lastDeltaZ;
    }

    private InstrumentState findByRole(String role) {
        for (Map.Entry<String, InstrumentState> e : byAlias.entrySet()) {
            if (role.equals(e.getValue().role)) {
                return e.getValue();
            }
        }
        // Fallback: primary alias treated as NQ when attached on NQ
        if ("NQ".equals(role)) {
            InstrumentState primary = byAlias.get(primaryAlias);
            if (primary != null && "OTHER".equals(primary.role)) {
                primary.role = "NQ";
                return primary;
            }
        }
        return null;
    }

    private static int sign(double z, double threshold) {
        if (z >= threshold) {
            return 1;
        }
        if (z <= -threshold) {
            return -1;
        }
        return 0;
    }

    private static String classifySymbol(InstrumentInfo info) {
        String symbol = info.symbol != null ? info.symbol.toUpperCase(Locale.ROOT) : "";
        String full = info.fullName != null ? info.fullName.toUpperCase(Locale.ROOT) : "";
        String combined = symbol + " " + full;
        if (combined.matches(".*\\bM?NQ\\b.*") || combined.contains("NASDAQ")) {
            return "NQ";
        }
        if (combined.matches(".*\\bM?ES\\b.*") || combined.contains("S&P") || combined.contains("SP500")) {
            return "ES";
        }
        // Rithmic-style aliases often look like "NQH6" / "ESM6"
        if (symbol.matches("^M?NQ[FGHJKMNQUVXZ]\\d.*") || symbol.startsWith("NQ")) {
            return "NQ";
        }
        if (symbol.matches("^M?ES[FGHJKMNQUVXZ]\\d.*") || symbol.startsWith("ES")) {
            return "ES";
        }
        return "OTHER";
    }

    @Override
    public void stop() {
        byAlias.clear();
        roleByAlias.clear();
    }
}
