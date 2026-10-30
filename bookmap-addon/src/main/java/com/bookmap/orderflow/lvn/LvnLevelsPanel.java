package com.bookmap.orderflow.lvn;

import java.awt.BorderLayout;
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
    private final JTextArea statusArea = new JTextArea(3, 60);
    private final JLabel eventLabel = new JLabel(" ");

    private final JSpinner esWindow = new JSpinner(new SpinnerNumberModel(90, 1, 400, 1));
    private final JSpinner esSens = new JSpinner(new SpinnerNumberModel(15, 1, 500, 1));
    private final JTextField esJoin = new JTextField("13:55", 6);
    private final JTextField esDopOpen = new JTextField("14:30", 6);
    private final JSpinner esLvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esLvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esHvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esHvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner esRefresh = new JSpinner(new SpinnerNumberModel(5, 1, 30, 1));

    private final JSpinner nqWindow = new JSpinner(new SpinnerNumberModel(30, 1, 400, 1));
    private final JSpinner nqSens = new JSpinner(new SpinnerNumberModel(100, 1, 500, 1));
    private final JTextField nqJoin = new JTextField("13:55", 6);
    private final JTextField nqDopOpen = new JTextField("14:30", 6);
    private final JSpinner nqLvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqLvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqHvnUp = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqHvnDn = new JSpinner(new SpinnerNumberModel(3, 0, 6, 1));
    private final JSpinner nqRefresh = new JSpinner(new SpinnerNumberModel(5, 1, 30, 1));

    private final JCheckBox showLvn = new JCheckBox("Show LVNs", true);
    private final JCheckBox showHvn = new JCheckBox("Show HVNs", true);
    private final JCheckBox showDop = new JCheckBox("Show DOP", true);

    private File lastDir = new File("C:\\Work\\Mat\\bookmap\\data\\irt");
    private final Consumer<LvnLevelsState.Snapshot> listener = this::refreshUi;
    private boolean syncing;

    private LvnLevelsPanel() {
        frame = new JFrame("LVN Levels");
        frame.setAlwaysOnTop(true);
        frame.setDefaultCloseOperation(WindowConstants.HIDE_ON_CLOSE);

        JPanel root = new JPanel();
        root.setLayout(new BoxLayout(root, BoxLayout.Y_AXIS));
        root.setBorder(new EmptyBorder(8, 10, 8, 10));

        JPanel top = new JPanel(new FlowLayout(FlowLayout.LEFT, 6, 4));
        JButton loadEs = new JButton("Load ES seed...");
        JButton loadNq = new JButton("Load NQ seed...");
        JButton applyEs = new JButton("Apply ES");
        JButton applyNq = new JButton("Apply NQ");
        loadEs.addActionListener(e -> chooseSeed("ES"));
        loadNq.addActionListener(e -> chooseSeed("NQ"));
        applyEs.addActionListener(e -> apply("ES"));
        applyNq.addActionListener(e -> apply("NQ"));
        top.add(loadEs);
        top.add(loadNq);
        top.add(applyEs);
        top.add(applyNq);
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

        statusArea.setEditable(false);
        statusArea.setLineWrap(true);
        statusArea.setWrapStyleWord(true);
        statusArea.setFont(statusArea.getFont().deriveFont(Font.PLAIN, 12f));
        statusArea.setText("Load VAP seed (ES / NQ). Attach the indicator on each chart you want lines on.");
        JScrollPane statusScroll = new JScrollPane(statusArea);
        statusScroll.setPreferredSize(new Dimension(820, 72));
        statusScroll.setAlignmentX(0f);

        eventLabel.setFont(eventLabel.getFont().deriveFont(Font.ITALIC, 11f));
        eventLabel.setAlignmentX(0f);

        JLabel hint = new JLabel(
                "Colors: LVN cyan | HVN lime | DOP dark blue.  Join UK = seed handoff HH:mm.");
        hint.setAlignmentX(0f);

        root.add(top);
        root.add(Box.createVerticalStrut(6));
        root.add(esBox);
        root.add(Box.createVerticalStrut(6));
        root.add(nqBox);
        root.add(Box.createVerticalStrut(6));
        root.add(hint);
        root.add(Box.createVerticalStrut(4));
        root.add(statusScroll);
        root.add(Box.createVerticalStrut(2));
        root.add(eventLabel);

        frame.setContentPane(root);
        frame.setMinimumSize(new Dimension(860, 420));
        frame.pack();
        frame.setSize(Math.max(frame.getWidth(), 900), Math.max(frame.getHeight(), 460));

        LvnLevelsState.addListener(listener);
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
                statusArea.setText(snap.statusDetail == null ? "" : snap.statusDetail);
                eventLabel.setText(snap.lastEvent == null ? "" : snap.lastEvent);
            } finally {
                syncing = false;
            }
        });
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
        join.setText(k.joinUkHm == null || k.joinUkHm.isBlank() ? "13:55" : k.joinUkHm);
        dop.setText(k.dopOpenUkHm == null || k.dopOpenUkHm.isBlank() ? "14:30" : k.dopOpenUkHm);
        lvnUp.setValue(k.lvnAbove);
        lvnDn.setValue(k.lvnBelow);
        hvnUp.setValue(k.hvnAbove);
        hvnDn.setValue(k.hvnBelow);
        refresh.setValue(k.refreshMin);
    }

    public static void showPanel() {
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
