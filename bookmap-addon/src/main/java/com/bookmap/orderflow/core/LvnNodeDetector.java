package com.bookmap.orderflow.core;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * IRT Profile-style LVN/HVN detection (devNodes / sensitivity).
 *
 * <p>Mirrors {@code research/irt_lvn/lvn_profile.py}: sensitivity {@code S} is
 * the number of price rows (±S) that must be strictly higher (HVN) or lower
 * (LVN) than the candidate. Row height = {@code tick} (0.25 for ES/NQ).
 */
public final class LvnNodeDetector {

    public static final double DEFAULT_TICK = 0.25;

    public static final class Node {
        public final double price;
        public final double volume;
        public final String kind; // "LVN" | "HVN"

        public Node(double price, double volume, String kind) {
            this.price = price;
            this.volume = volume;
            this.kind = kind;
        }
    }

    public static final class Selection {
        /** Nearest first (index 0 = closest to mid). */
        public final List<Double> above;
        public final List<Double> below;

        public Selection(List<Double> above, List<Double> below) {
            this.above = Collections.unmodifiableList(new ArrayList<>(above));
            this.below = Collections.unmodifiableList(new ArrayList<>(below));
        }

        public List<Double> allSorted() {
            List<Double> out = new ArrayList<>(below.size() + above.size());
            out.addAll(below);
            out.addAll(above);
            Collections.sort(out);
            return out;
        }
    }

    private LvnNodeDetector() {}

    public static int priceToRow(double px, double tick) {
        return (int) Math.round(px / tick);
    }

    public static double rowToPrice(int row, double tick) {
        return row * tick;
    }

    /**
     * Detect valleys (LVN) and peaks (HVN) from a row→volume map.
     *
     * @return [0]=valleys, [1]=peaks
     */
    @SuppressWarnings("unchecked")
    public static List<Node>[] detect(
            Map<Integer, Double> vap,
            int sensitivity,
            double tick,
            boolean strict,
            boolean skipEdge) {
        List<Node> valleys = new ArrayList<>();
        List<Node> peaks = new ArrayList<>();
        @SuppressWarnings("rawtypes")
        List[] out = new List[] {valleys, peaks};
        if (vap == null || vap.isEmpty() || sensitivity < 1) {
            return out;
        }
        TreeMap<Integer, Double> vol = new TreeMap<>();
        for (Map.Entry<Integer, Double> e : vap.entrySet()) {
            if (e.getValue() != null && e.getValue() > 0) {
                vol.put(e.getKey(), e.getValue());
            }
        }
        if (vol.size() < 2 * sensitivity + 1) {
            return out;
        }
        int lo = vol.firstKey();
        int hi = vol.lastKey();
        for (Map.Entry<Integer, Double> e : vol.entrySet()) {
            int r = e.getKey();
            double v = e.getValue();
            if (skipEdge && (r - lo < sensitivity || hi - r < sensitivity)) {
                continue;
            }
            if (v <= 0) {
                continue;
            }
            boolean isValley = true;
            boolean isPeak = true;
            for (int k = 1; k <= sensitivity; k++) {
                double above = vol.getOrDefault(r + k, 0.0);
                double below = vol.getOrDefault(r - k, 0.0);
                if (strict) {
                    if (!(v < above && v < below)) {
                        isValley = false;
                    }
                    if (!(v > above && v > below)) {
                        isPeak = false;
                    }
                } else {
                    if (!(v <= above && v <= below)) {
                        isValley = false;
                    }
                    if (!(v >= above && v >= below)) {
                        isPeak = false;
                    }
                }
                if (!isValley && !isPeak) {
                    break;
                }
            }
            if (isValley && !isPeak) {
                valleys.add(new Node(rowToPrice(r, tick), v, "LVN"));
            } else if (isPeak && !isValley) {
                peaks.add(new Node(rowToPrice(r, tick), v, "HVN"));
            }
        }
        return out;
    }

    public static List<Node>[] detect(Map<Integer, Double> vap, int sensitivity) {
        return detect(vap, sensitivity, DEFAULT_TICK, true, true);
    }

    /** Nearest {@code n} nodes to {@code ref} (nearest first). */
    public static List<Node> nearestN(List<Node> nodes, double ref, int n) {
        if (nodes == null || nodes.isEmpty() || n <= 0) {
            return List.of();
        }
        List<Node> ranked = new ArrayList<>(nodes);
        ranked.sort((a, b) -> Double.compare(Math.abs(a.price - ref), Math.abs(b.price - ref)));
        if (ranked.size() > n) {
            return new ArrayList<>(ranked.subList(0, n));
        }
        return ranked;
    }

    /**
     * Split nodes into nearest-N strictly above and below {@code mid}.
     * Above sorted low→high (nearest first); below high→low (nearest first).
     */
    public static Selection selectAround(List<Node> nodes, double mid, int nAbove, int nBelow) {
        List<Double> below = new ArrayList<>();
        List<Double> above = new ArrayList<>();
        if (nodes != null) {
            for (Node nd : nodes) {
                if (nd.price < mid - 1e-9) {
                    below.add(nd.price);
                } else if (nd.price > mid + 1e-9) {
                    above.add(nd.price);
                }
            }
        }
        below.sort((a, b) -> Double.compare(b, a)); // nearest first
        above.sort(Double::compare); // nearest first
        if (below.size() > Math.max(0, nBelow)) {
            below = new ArrayList<>(below.subList(0, nBelow));
        }
        if (above.size() > Math.max(0, nAbove)) {
            above = new ArrayList<>(above.subList(0, nAbove));
        }
        return new Selection(above, below);
    }

    /** All node prices (any kind) for confluence detectors. */
    public static List<Double> pricesOf(List<Node> nodes) {
        if (nodes == null || nodes.isEmpty()) {
            return List.of();
        }
        List<Double> out = new ArrayList<>(nodes.size());
        for (Node n : nodes) {
            out.add(n.price);
        }
        Collections.sort(out);
        return out;
    }
}
