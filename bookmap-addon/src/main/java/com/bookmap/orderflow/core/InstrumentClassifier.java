package com.bookmap.orderflow.core;

import java.util.Locale;

import velox.api.layer1.data.InstrumentInfo;

/** Classify Bookmap instruments as NQ / ES / OTHER (including micros as NQ/ES). */
public final class InstrumentClassifier {

    private InstrumentClassifier() {}

    public static String classify(InstrumentInfo info) {
        String symbol = info.symbol != null ? info.symbol.toUpperCase(Locale.ROOT) : "";
        String full = info.fullName != null ? info.fullName.toUpperCase(Locale.ROOT) : "";
        String combined = symbol + " " + full;
        if (combined.matches(".*\\bM?NQ\\b.*") || combined.contains("NASDAQ")) {
            return "NQ";
        }
        if (combined.matches(".*\\bM?ES\\b.*") || combined.contains("S&P") || combined.contains("SP500")) {
            return "ES";
        }
        if (symbol.matches("^M?NQ[FGHJKMNQUVXZ]\\d.*") || symbol.startsWith("NQ")) {
            return "NQ";
        }
        if (symbol.matches("^M?ES[FGHJKMNQUVXZ]\\d.*") || symbol.startsWith("ES")) {
            return "ES";
        }
        return "OTHER";
    }
}
