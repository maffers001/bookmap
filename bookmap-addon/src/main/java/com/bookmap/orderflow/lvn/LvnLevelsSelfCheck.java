package com.bookmap.orderflow.lvn;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import com.bookmap.orderflow.core.LvnLiveVap;
import com.bookmap.orderflow.core.LvnNodeDetector;
import com.bookmap.orderflow.core.LvnVapSeedFile;

/**
 * Bookmap-free smoke check: synthetic VAP extrema + optional seed directory load.
 *
 * <pre>
 * java -cp build/classes/java/main com.bookmap.orderflow.lvn.LvnLevelsSelfCheck [seedDir]
 * </pre>
 */
public final class LvnLevelsSelfCheck {

    public static void main(String[] args) throws Exception {
        int fails = 0;
        fails += checkSynthetic();
        if (args.length > 0) {
            fails += checkSeed(Path.of(args[0]));
        } else {
            Path def = Path.of("c:/Work/Mat/bookmap/data/irt/vap_seed_ES");
            if (Files.isDirectory(def) && Files.isRegularFile(def.resolve("meta.json"))) {
                fails += checkSeed(def);
            } else {
                System.out.println("SKIP seed load (no vap_seed_ES yet)");
            }
        }
        if (fails > 0) {
            System.err.println("FAILED checks=" + fails);
            System.exit(1);
        }
        System.out.println("OK LvnLevelsSelfCheck");
    }

    private static int checkSynthetic() {
        // Flat 10 vol with a valley at row 100 and peak at 120; S=2
        Map<Integer, Double> vap = new HashMap<>();
        for (int r = 90; r <= 130; r++) {
            vap.put(r, 10.0);
        }
        vap.put(100, 1.0); // valley
        vap.put(120, 50.0); // peak
        List<LvnNodeDetector.Node>[] nodes = LvnNodeDetector.detect(vap, 2, 0.25, true, true);
        List<LvnNodeDetector.Node> valleys = nodes[0];
        List<LvnNodeDetector.Node> peaks = nodes[1];
        boolean okV = valleys.stream().anyMatch(n -> Math.abs(n.price - 25.0) < 1e-9);
        boolean okP = peaks.stream().anyMatch(n -> Math.abs(n.price - 30.0) < 1e-9);
        System.out.printf(Locale.US, "synthetic valleys=%d peaks=%d valley@25=%s peak@30=%s%n",
                valleys.size(), peaks.size(), okV, okP);
        return (okV && okP) ? 0 : 1;
    }

    private static int checkSeed(Path dir) throws Exception {
        LvnVapSeedFile.Seed seed = LvnVapSeedFile.load(dir);
        LvnLiveVap vap = new LvnLiveVap();
        vap.loadSeed(seed);
        int window = Math.max(1, seed.meta.windowDaysAvailable);
        // Use market default sensitivity from filename
        int sens = dir.getFileName().toString().contains("NQ") ? 100 : 15;
        LvnLiveVap.NodesSnapshot snap = vap.rebuild(Math.min(window, 90), sens, seed.meta.throughUk);
        System.out.printf(Locale.US,
                "seed %s through=%s vapRows=%d LVN=%d HVN=%d windowUsed=%d warn=%s%n",
                seed.meta.market, seed.meta.throughUk, snap.vap.size(),
                snap.valleys.size(), snap.peaks.size(), snap.windowDaysUsed, snap.warning);
        if (snap.vap.isEmpty()) {
            return 1;
        }
        // Join gate: trade before through ignored
        LocalDateTime before = seed.meta.throughUk.minusMinutes(5);
        boolean rejected = !vap.onTrade(30000, 1, before);
        LocalDateTime after = seed.meta.throughUk.plusMinutes(1);
        boolean accepted = vap.onTrade(30000, 5, after);
        System.out.printf(Locale.US, "join gate rejectBefore=%s acceptAfter=%s%n", rejected, accepted);
        return (rejected && accepted) ? 0 : 1;
    }
}
