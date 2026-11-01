package com.bookmap.orderflow.lvn;

import java.awt.Color;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Insets;
import java.io.File;
import java.util.function.Consumer;

import javax.swing.BorderFactory;
import javax.swing.Box;
import javax.swing.BoxLayout;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JComboBox;
import javax.swing.JFileChooser;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSpinner;
import javax.swing.JTextArea;
import javax.swing.JTextField;
import javax.swing.SpinnerNumberModel;
import javax.swing.SwingUtilities;
import javax.swing.WindowConstants;
import javax.swing.border.EmptyBorder;
import javax.swing.border.TitledBorder;
import javax.swing.filechooser.FileNameExtensionFilter;

/**
 * Always-on-top console for LVN Levels: load seed, IRT-style knobs, show toggles.
 * One process-wide panel shared by ES and NQ attaches.
 */
public final class LvnLevelsPanel {

    private static LvnLevelsPanel instance;

    private final JFrame frame;
    private final JLabel seedBannerLabel = new JLabel(" ");
    private final JTextArea statusArea = new JTextArea(3, 60);
    private final JLabel eventLabel = new JLabel(" ");
    private final JLabel alertFooterLabel = new JLabel("Alert: (none yet)");

    private final JSpinner esWindow = new JSpinner(new SpinnerNumberModel(90, 1, 400, 1));
    private final JSpinner esSens = new JSpinner(new SpinnerNumberModel(15, 1, 500, 1));
    private final JTextField esJoin = new JTextField(LvnLevelsState.DEFAULT_JOIN_UK_HM, 6);
    private final JTextField esDopOpen = new JTextField(LvnLevelsState.DEFAULT_DOP_OPEN_UK_HM, 6);
    private final JSpinner esLvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esLvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esHvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esHvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esRefresh = new JSpinner(new SpinnerNumberModel(5, 1, 30, 1));

    private final JSpinner nqWindow = new JSpinner(new SpinnerNumberModel(30, 1, 400, 1));
    private final JSpinner nqSens = new JSpinner(new SpinnerNumberModel(100, 1, 500, 1));
    private final JTextField nqJoin = new JTextField(LvnLevelsState.DEFAULT_JOIN_UK_HM, 6);
    private final JTextField nqDopOpen = new JTextField(LvnLevelsState.DEFAULT_DOP_OPEN_UK_HM, 6);
    private final JSpinner nqLvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqLvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqHvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqHvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqRefresh = new JSpinner(new SpinnerNumberModel(5, 1, 30, 1));

    private final JCheckBox showLvn = new JCheckBox("Show LVNs", true);
    private final JCheckBox showHvn = new JCheckBox("Show HVNs", true);
    private final JCheckBox showDop = new JCheckBox("Show DOP", true);

    private final JCheckBox alertEnabled = new JCheckBox("Approach alert", false);
    private final JComboBox<String> alertMode = new JComboBox<>(new String[] {
            "Correlated", "Either market", "Single ES", "Single NQ"
    });
    private final JSpinner alertEsApproach = new JSpinner(new SpinnerNumberModel(5.0, 0.25, 200.0, 0.25));
    private final JSpinner alertEsPullback = new JSpinner(new SpinnerNumberModel(3.0, 0.0, 200.0, 0.25));
    private final JSpinner alertNqApproach = new JSpinner(new SpinnerNumberModel(20.0, 0.25, 500.0, 1.0));
    private final JSpinner alertNqPullback = new JSpinner(new SpinnerNumberModel(10.0, 0.0, 500.0, 1.0));
    private final JSpinner alertCooldown = new JSpinner(new SpinnerNumberModel(60, 0, 3600, 5));
    private final JSpinner alertEsProx = new JSpinner(new SpinnerNumberModel(5.0, 0.25, 200.0, 0.25));
    private final JSpinner alertNqProx = new JSpinner(new SpinnerNumberModel(20.0, 0.25, 500.0, 1.0));
    private final JCheckBox alertWatchLvn = new JCheckBox("LVN", true);
    private final JCheckBox alertWatchDop = new JCheckBox("DOP", true);
    private final JCheckBox alertSound = new JCheckBox("Sound", true);

    private File lastDir = LvnLevelsState.DEFAULT_SEED_ROOT.toFile();
    private final Consumer<LvnLevelsState.Snapshot> listener = this::refreshUi;
    private boolean syncing;
    private JButton rebuildSeedBtn;

    private LvnLevelsPanel() {
        frame = new JFrame("LVN Levels");
        frame.setAlwaysOnTop(true);
        frame.setDefaultCloseOperation(WindowConstants.HIDE_ON_CLOSE);

        JPanel root = new JPanel();
        root.setLayout(new BoxLayout(root, BoxLayout.Y_AXIS));
        root.setBorder(new EmptyBorder(8, 10, 8, 10));

        seedBannerLabel.setOpaque(true);
        seedBannerLabel.setBackground(new Color(160, 32, 32));
        seedBannerLabel.setForeground(Color.WHITE);
        seedBannerLabel.setFont(seedBannerLabel.getFont().deriveFont(Font.BOLD, 12f));
        seedBannerLabel.setBorder(new EmptyBorder(8, 10, 8, 10));
        seedBannerLabel.setAlignmentX(0f);
        seedBannerLabel.setVisible(false);

        JPanel top = new JPanel(new FlowLayout(FlowLayout.LEFT, 6, 4));
        JButton loadEs = new JButton("Load ES seed...");
        JButton loadNq = new JButton("Load NQ seed...");
        JButton applyEs = new JButton("Apply ES");
        JButton applyNq = new JButton("Apply NQ");
        rebuildSeedBtn = new JButton("Rebuild seed for session");
        rebuildSeedBtn.setToolTipText(
                "Also runs automatically when the add-on is added. "
                        + "Re-run build_vap_seed for the UK session day using Join UK time, then reload ES+NQ");
        loadEs.addActionListener(e -> chooseSeed("ES"));
        loadNq.addActionListener(e -> chooseSeed("NQ"));
        applyEs.addActionListener(e -> apply("ES"));
        applyNq.addActionListener(e -> apply("NQ"));
        rebuildSeedBtn.addActionListener(e -> rebuildSeedsForSession());
        top.add(loadEs);
        top.add(loadNq);
        top.add(applyEs);
        top.add(applyNq);
        top.add(rebuildSeedBtn);
        top.add(showLvn);
        top.add(showHvn);
        top.add(showDop);
        top.setAlignmentX(0f);
        showLvn.addActionListener(e -> {
            if (!syncing) {
                LvnLevelsState.setShowLvn(showLvn.isSelected(), "panel");
            }
        });
        showHvn.addActionListener(e -> {
            if (!syncing) {
                LvnLevelsState.setShowHvn(showHvn.isSelected(), "panel");
            }
        });
        showDop.addActionListener(e -> {
            if (!syncing) {
                LvnLevelsState.setShowDop(showDop.isSelected(), "panel");
            }
        });

        JPanel esBox = marketBox("ES (attach LVN Levels on ES chart)",
                esWindow, esSens, esJoin, esDopOpen,
                esLvnUp, esLvnDn, esHvnUp, esHvnDn, esRefresh);
        JPanel nqBox = marketBox("NQ (attach LVN Levels on NQ chart — same console)",
                nqWindow, nqSens, nqJoin, nqDopOpen,
                nqLvnUp, nqLvnDn, nqHvnUp, nqHvnDn, nqRefresh);
        JPanel alertBox = alertBox();

        statusArea.setEditable(false);
        statusArea.setLineWrap(true);
        statusArea.setWrapStyleWord(true);
        statusArea.setFont(statusArea.getFont().deriveFont(Font.PLAIN, 12f));
        statusArea.setText("Auto-loading vap_seed_ES / vap_seed_NQ from "
                + LvnLevelsState.DEFAULT_SEED_ROOT + ". Attach on each chart for lines.");
        JScrollPane statusScroll = new JScrollPane(statusArea);
        statusScroll.setPreferredSize(new Dimension(820, 72));
        statusScroll.setAlignmentX(0f);

        eventLabel.setFont(eventLabel.getFont().deriveFont(Font.ITALIC, 11f));
        eventLabel.setAlignmentX(0f);

        alertFooterLabel.setOpaque(true);
        alertFooterLabel.setBackground(new Color(32, 32, 32));
        alertFooterLabel.setForeground(new Color(255, 220, 80));
        alertFooterLabel.setFont(alertFooterLabel.getFont().deriveFont(Font.BOLD, 14f));
        alertFooterLabel.setBorder(BorderFactory.createCompoundBorder(
                BorderFactory.createMatteBorder(1, 0, 0, 0, new Color(90, 90, 90)),
                new EmptyBorder(8, 6, 6, 6)));
        alertFooterLabel.setAlignmentX(0f);
        alertFooterLabel.setToolTipText(
                "Last approach alert reason. Correlated example: ES cross DOP, NQ approach LVN");

        JLabel hint = new JLabel(
                "Colors: LVN cyan | HVN lime | DOP dark blue.  Join UK = seed handoff HH:mm.  "
                        + "Correlated = same-direction S/R: one market enters Approach, the other is "
                        + "within Prox on the same side, both still within Prox and not through the level.");
        hint.setAlignmentX(0f);

        root.add(seedBannerLabel);
        root.add(Box.createVerticalStrut(6));
        root.add(top);
        root.add(Box.createVerticalStrut(6));
        root.add(esBox);
        root.add(Box.createVerticalStrut(6));
        root.add(nqBox);
        root.add(Box.createVerticalStrut(6));
        root.add(alertBox);
        root.add(Box.createVerticalStrut(6));
        root.add(hint);
        root.add(Box.createVerticalStrut(4));
        root.add(statusScroll);
        root.add(Box.createVerticalStrut(2));
        root.add(eventLabel);
        root.add(Box.createVerticalStrut(4));
        root.add(alertFooterLabel);

        frame.setContentPane(root);
        frame.setMinimumSize(new Dimension(860, 520));
        frame.pack();
        frame.setSize(Math.max(frame.getWidth(), 920), Math.max(frame.getHeight(), 560));

        LvnLevelsState.addListener(listener);
    }

    private JPanel alertBox() {
        JPanel box = new JPanel(new GridBagLayout());
        box.setBorder(BorderFactory.createTitledBorder(
                BorderFactory.createEtchedBorder(),
                "Approach alert (displayed LVNs / DOP)",
                TitledBorder.LEFT, TitledBorder.TOP));
        box.setAlignmentX(0f);
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(3, 6, 3, 6);
        c.anchor = GridBagConstraints.WEST;

        alertEnabled.addActionListener(e -> {
            if (!syncing) {
                applyAlert();
            }
        });
        java.awt.event.ItemListener liveApply = e -> {
            if (!syncing && alertEnabled.isSelected()) {
                applyAlert();
            }
        };
        alertMode.addItemListener(liveApply);
        alertWatchLvn.addItemListener(liveApply);
        alertWatchDop.addItemListener(liveApply);
        alertSound.addItemListener(liveApply);

        int col = 0;
        c.gridy = 0;
        c.gridx = col++;
        box.add(alertEnabled, c);
        c.gridx = col++;
        box.add(new JLabel("Mode"), c);
        c.gridx = col++;
        alertMode.setPrototypeDisplayValue("Either market");
        box.add(alertMode, c);
        c.gridx = col++;
        box.add(alertWatchLvn, c);
        c.gridx = col++;
        box.add(alertWatchDop, c);
        c.gridx = col++;
        box.add(alertSound, c);
        c.gridx = col++;
        JButton applyAlert = new JButton("Apply alert");
        applyAlert.addActionListener(e -> applyAlert());
        box.add(applyAlert, c);

        alertEsApproach.setToolTipText("ES: must enter from outside this many chart pts of the level");
        alertEsPullback.setToolTipText("ES: invalidate if mid moves this many pts away from the level during approach (0 = off)");
        alertNqApproach.setToolTipText("NQ: must enter from outside this many chart pts of the level");
        alertNqPullback.setToolTipText("NQ: invalidate if mid moves this many pts away from the level during approach (0 = off)");
        alertEsProx.setToolTipText("Correlated: when NQ crosses, ES mid must be within this many pts of an ES LVN/DOP");
        alertNqProx.setToolTipText("Correlated: when ES crosses, NQ mid must be within this many pts of an NQ LVN/DOP");

        col = 0;
        c.gridy = 1;
        c.gridx = col++;
        box.add(new JLabel("ES Approach"), c);
        c.gridx = col++;
        box.add(alertEsApproach, c);
        c.gridx = col++;
        box.add(new JLabel("ES Pullback"), c);
        c.gridx = col++;
        box.add(alertEsPullback, c);
        c.gridx = col++;
        box.add(new JLabel("ES Prox"), c);
        c.gridx = col++;
        box.add(alertEsProx, c);
        c.gridx = col++;
        box.add(new JLabel("Cooldown s"), c);
        c.gridx = col++;
        box.add(alertCooldown, c);

        col = 0;
        c.gridy = 2;
        c.gridx = col++;
        box.add(new JLabel("NQ Approach"), c);
        c.gridx = col++;
        box.add(alertNqApproach, c);
        c.gridx = col++;
        box.add(new JLabel("NQ Pullback"), c);
        c.gridx = col++;
        box.add(alertNqPullback, c);
        c.gridx = col++;
        box.add(new JLabel("NQ Prox"), c);
        c.gridx = col++;
        box.add(alertNqProx, c);
        return box;
    }

    private static JPanel marketBox(
            String title,
            JSpinner window, JSpinner sens, JTextField join, JTextField dop,
            JSpinner lvnUp, JSpinner lvnDn, JSpinner hvnUp, JSpinner hvnDn, JSpinner refresh) {
        JPanel box = new JPanel(new GridBagLayout());
        box.setBorder(BorderFactory.createTitledBorder(
                BorderFactory.createEtchedBorder(), title, TitledBorder.LEFT, TitledBorder.TOP));
        box.setAlignmentX(0f);
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(3, 6, 3, 6);
        c.anchor = GridBagConstraints.WEST;
        c.fill = GridBagConstraints.NONE;

        int col = 0;
        c.gridy = 0;
        c.gridx = col++;
        box.add(new JLabel("Window d"), c);
        c.gridx = col++;
        box.add(window, c);
        c.gridx = col++;
        box.add(new JLabel("Sens"), c);
        c.gridx = col++;
        box.add(sens, c);
        c.gridx = col++;
        box.add(new JLabel("Join UK"), c);
        c.gridx = col++;
        join.setToolTipText("HH:mm UK — seed ends; Bookmap volume starts after");
        box.add(join, c);
        c.gridx = col++;
        box.add(new JLabel("DOP open"), c);
        c.gridx = col++;
        dop.setToolTipText("HH:mm UK — first trade at/after this time = DOP");
        box.add(dop, c);

        col = 0;
        c.gridy = 1;
        c.gridx = col++;
        box.add(new JLabel("LVN up/dn"), c);
        c.gridx = col++;
        JPanel lvn = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 0));
        lvn.add(lvnUp);
        lvn.add(lvnDn);
        box.add(lvn, c);
        c.gridx = col++;
        box.add(new JLabel("HVN up/dn"), c);
        c.gridx = col++;
        JPanel hvn = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 0));
        hvn.add(hvnUp);
        hvn.add(hvnDn);
        box.add(hvn, c);
        c.gridx = col++;
        box.add(new JLabel("Refresh m"), c);
        c.gridx = col++;
        box.add(refresh, c);
        return box;
    }

    private void chooseSeed(String market) {
        JFileChooser fc = new JFileChooser(lastDir);
        fc.setFileSelectionMode(JFileChooser.FILES_AND_DIRECTORIES);
        fc.setDialogTitle("Select vap_seed_" + market + " directory (or meta.json)");
        fc.setFileFilter(new FileNameExtensionFilter("meta.json", "json"));
        if (fc.showOpenDialog(frame) != JFileChooser.APPROVE_OPTION) {
            return;
        }
        File f = fc.getSelectedFile();
        if (f != null) {
            lastDir = f.isDirectory() ? f : f.getParentFile();
            LvnLevelsState.bindSeed(market, f.toPath(), "panel");
        }
    }

    private void apply(String market) {
        LvnLevelsState.MarketKnobs k = LvnLevelsState.knobsFor(market);
        if ("NQ".equals(market)) {
            k.windowDays = (Integer) nqWindow.getValue();
            k.sensitivity = (Integer) nqSens.getValue();
            k.joinUkHm = nqJoin.getText();
            k.dopOpenUkHm = nqDopOpen.getText();
            k.lvnAbove = (Integer) nqLvnUp.getValue();
            k.lvnBelow = (Integer) nqLvnDn.getValue();
            k.hvnAbove = (Integer) nqHvnUp.getValue();
            k.hvnBelow = (Integer) nqHvnDn.getValue();
            k.refreshMin = (Integer) nqRefresh.getValue();
        } else {
            k.windowDays = (Integer) esWindow.getValue();
            k.sensitivity = (Integer) esSens.getValue();
            k.joinUkHm = esJoin.getText();
            k.dopOpenUkHm = esDopOpen.getText();
            k.lvnAbove = (Integer) esLvnUp.getValue();
            k.lvnBelow = (Integer) esLvnDn.getValue();
            k.hvnAbove = (Integer) esHvnUp.getValue();
            k.hvnBelow = (Integer) esHvnDn.getValue();
            k.refreshMin = (Integer) esRefresh.getValue();
        }
        LvnLevelsState.applyKnobs(market, k, "panel");
    }

    private void rebuildSeedsForSession() {
        // Prefer ES Join; keep NQ in sync so both markets share the seed cut.
        String through = esJoin.getText();
        if (through == null || through.isBlank()) {
            through = LvnLevelsState.DEFAULT_JOIN_UK_HM;
        }
        nqJoin.setText(through.trim());
        apply("ES");
        apply("NQ");
        if (rebuildSeedBtn != null) {
            rebuildSeedBtn.setEnabled(false);
            rebuildSeedBtn.setText("Rebuilding seed…");
        }
        LvnLevelsState.rebuildSeedsForSession(through.trim(), msg -> SwingUtilities.invokeLater(() -> {
            if (rebuildSeedBtn != null) {
                rebuildSeedBtn.setEnabled(!LvnLevelsState.seedRebuildBusy());
                rebuildSeedBtn.setText("Rebuild seed for session");
            }
            eventLabel.setText(msg == null ? "" : msg);
        }));
    }

    private void applyAlert() {
        LvnApproachAlert.Config c = new LvnApproachAlert.Config();
        c.enabled = alertEnabled.isSelected();
        c.mode = modeFromUi((String) alertMode.getSelectedItem());
        c.esApproachPts = ((Number) alertEsApproach.getValue()).doubleValue();
        c.esPullbackPts = ((Number) alertEsPullback.getValue()).doubleValue();
        c.nqApproachPts = ((Number) alertNqApproach.getValue()).doubleValue();
        c.nqPullbackPts = ((Number) alertNqPullback.getValue()).doubleValue();
        c.cooldownSec = (Integer) alertCooldown.getValue();
        c.esCorrProxPts = ((Number) alertEsProx.getValue()).doubleValue();
        c.nqCorrProxPts = ((Number) alertNqProx.getValue()).doubleValue();
        c.watchLvn = alertWatchLvn.isSelected();
        c.watchDop = alertWatchDop.isSelected();
        c.sound = alertSound.isSelected();
        LvnLevelsState.applyAlertConfig(c, "panel");
    }

    private static LvnApproachAlert.Mode modeFromUi(String label) {
        if (label == null) {
            return LvnApproachAlert.Mode.CORRELATED;
        }
        switch (label) {
            case "Single ES":
                return LvnApproachAlert.Mode.ES;
            case "Single NQ":
                return LvnApproachAlert.Mode.NQ;
            case "Either market":
                return LvnApproachAlert.Mode.EITHER;
            case "Correlated":
            default:
                return LvnApproachAlert.Mode.CORRELATED;
        }
    }

    private static String modeToUi(LvnApproachAlert.Mode mode) {
        if (mode == null) {
            return "Correlated";
        }
        switch (mode) {
            case ES:
                return "Single ES";
            case NQ:
                return "Single NQ";
            case EITHER:
                return "Either market";
            case CORRELATED:
            default:
                return "Correlated";
        }
    }

    private void refreshUi(LvnLevelsState.Snapshot snap) {
        SwingUtilities.invokeLater(() -> {
            syncing = true;
            try {
                applyKnobsToUi(snap.esKnobs, esWindow, esSens, esJoin, esDopOpen,
                        esLvnUp, esLvnDn, esHvnUp, esHvnDn, esRefresh);
                applyKnobsToUi(snap.nqKnobs, nqWindow, nqSens, nqJoin, nqDopOpen,
                        nqLvnUp, nqLvnDn, nqHvnUp, nqHvnDn, nqRefresh);
                showLvn.setSelected(snap.showLvn);
                showHvn.setSelected(snap.showHvn);
                showDop.setSelected(snap.showDop);
                if (snap.alert != null) {
                    alertEnabled.setSelected(snap.alert.enabled);
                    alertMode.setSelectedItem(modeToUi(snap.alert.mode));
                    alertEsApproach.setValue(snap.alert.esApproachPts);
                    alertEsPullback.setValue(snap.alert.esPullbackPts);
                    alertNqApproach.setValue(snap.alert.nqApproachPts);
                    alertNqPullback.setValue(snap.alert.nqPullbackPts);
                    alertCooldown.setValue(snap.alert.cooldownSec);
                    alertEsProx.setValue(snap.alert.esCorrProxPts);
                    alertNqProx.setValue(snap.alert.nqCorrProxPts);
                    alertWatchLvn.setSelected(snap.alert.watchLvn);
                    alertWatchDop.setSelected(snap.alert.watchDop);
                    alertSound.setSelected(snap.alert.sound);
                }
                statusArea.setText(snap.statusDetail == null ? "" : snap.statusDetail);
                eventLabel.setText(snap.lastEvent == null ? "" : snap.lastEvent);
                if (snap.alertFooter == null || snap.alertFooter.isEmpty()) {
                    alertFooterLabel.setText("Alert: (none yet)");
                    alertFooterLabel.setForeground(new Color(160, 160, 160));
                } else {
                    alertFooterLabel.setText("Alert: " + snap.alertFooter);
                    alertFooterLabel.setForeground(new Color(255, 220, 80));
                }
                applySeedBanner(snap.seedBanner);
                if (rebuildSeedBtn != null) {
                    boolean busy = LvnLevelsState.seedRebuildBusy();
                    rebuildSeedBtn.setEnabled(!busy);
                    if (busy) {
                        rebuildSeedBtn.setText("Rebuilding seed…");
                    } else if (!"Rebuild seed for session".equals(rebuildSeedBtn.getText())) {
                        rebuildSeedBtn.setText("Rebuild seed for session");
                    }
                }
            } finally {
                syncing = false;
            }
        });
    }

    private void applySeedBanner(String banner) {
        if (banner == null || banner.isBlank()) {
            seedBannerLabel.setVisible(false);
            seedBannerLabel.setText(" ");
            return;
        }
        seedBannerLabel.setText("<html><body style='width:860px'>"
                + banner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                + "</body></html>");
        seedBannerLabel.setVisible(true);
    }

    private static void applyKnobsToUi(
            LvnLevelsState.MarketKnobs k,
            JSpinner window, JSpinner sens, JTextField join, JTextField dop,
            JSpinner lvnUp, JSpinner lvnDn, JSpinner hvnUp, JSpinner hvnDn, JSpinner refresh) {
        if (k == null) {
            return;
        }
        window.setValue(k.windowDays);
        sens.setValue(k.sensitivity);
        join.setText(k.joinUkHm == null || k.joinUkHm.isBlank()
                ? LvnLevelsState.DEFAULT_JOIN_UK_HM : k.joinUkHm);
        dop.setText(k.dopOpenUkHm == null || k.dopOpenUkHm.isBlank()
                ? LvnLevelsState.DEFAULT_DOP_OPEN_UK_HM : k.dopOpenUkHm);
        lvnUp.setValue(k.lvnAbove);
        lvnDn.setValue(k.lvnBelow);
        hvnUp.setValue(k.hvnAbove);
        hvnDn.setValue(k.hvnBelow);
        refresh.setValue(k.refreshMin);
    }

    public static void showPanel() {
        LvnLevelsState.ensureDefaultSeedsLoaded();
        SwingUtilities.invokeLater(() -> {
            if (instance == null) {
                instance = new LvnLevelsPanel();
            }
            instance.frame.setVisible(true);
            instance.frame.toFront();
        });
    }

    public static void hidePanel() {
        SwingUtilities.invokeLater(() -> {
            if (instance != null) {
                instance.frame.setVisible(false);
            }
        });
    }
}
