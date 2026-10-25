package com.bookmap.orderflow.trade;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.GridLayout;
import java.awt.Insets;
import java.awt.Toolkit;
import java.io.File;
import java.nio.file.Path;
import java.time.LocalDate;
import java.util.Locale;
import java.util.function.Consumer;

import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JFileChooser;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTable;
import javax.swing.JTextField;
import javax.swing.JToggleButton;
import javax.swing.SwingConstants;
import javax.swing.SwingUtilities;
import javax.swing.WindowConstants;
import javax.swing.filechooser.FileNameExtensionFilter;
import javax.swing.table.DefaultTableModel;

import com.bookmap.orderflow.core.LvnLevelFile;
import com.bookmap.orderflow.core.OpenClusterDetector.DisplayRow;

import velox.api.layer1.common.Log;

/**
 * Always-on-top console for LVN Confluence AutoTrade: load IRT LVN file,
 * enable/disable trading, adjust brackets, live LVN + large-order display.
 */
public final class LvnControlPanel {

    private static LvnControlPanel instance;

    private final JFrame frame;
    private final JLabel statusLabel;
    private final JLabel warnLabel;
    private final JLabel eventLabel;
    private final JLabel levelsLabel;
    private final JToggleButton pinToggle;
    private final JToggleButton tradingToggle;
    private final JToggleButton showChartToggle;
    private final JToggleButton showHvnToggle;
    private final JToggleButton longsToggle;
    private final JToggleButton shortsToggle;
    private final JTextField slField;
    private final JTextField tpField;
    private final JTextField offsetField;
    private final JTextField nearField;
    private final JTextField minOpenField;
    private final JTextField sizeField;
    private final JTextField entryUntilField;
    private final JTextField eosField;
    private final DefaultTableModel lvnModel;
    private final DefaultTableModel ordersModel;
    private final Consumer<LvnStrategyState.Snapshot> listener;
    private boolean updatingUi;
    private File lastDir = defaultExportDir();

    private LvnControlPanel() {
        frame = new JFrame("LVN Confluence");
        frame.setAlwaysOnTop(true);
        frame.setDefaultCloseOperation(WindowConstants.HIDE_ON_CLOSE);
        frame.setType(java.awt.Window.Type.UTILITY);

        statusLabel = new JLabel("NO LEVELS", SwingConstants.CENTER);
        statusLabel.setFont(statusLabel.getFont().deriveFont(Font.BOLD, 18f));
        statusLabel.setOpaque(true);
        statusLabel.setBorder(BorderFactory.createEmptyBorder(8, 12, 8, 12));

        warnLabel = new JLabel(" ", SwingConstants.CENTER);
        warnLabel.setForeground(new Color(0xB7, 0x1C, 0x1C));
        warnLabel.setFont(warnLabel.getFont().deriveFont(Font.BOLD, 11f));

        levelsLabel = new JLabel("No LVN file loaded", SwingConstants.CENTER);
        levelsLabel.setFont(levelsLabel.getFont().deriveFont(11f));

        eventLabel = new JLabel(" ", SwingConstants.CENTER);
        eventLabel.setFont(eventLabel.getFont().deriveFont(Font.ITALIC, 11f));

        pinToggle = new JToggleButton("Pin ON");
        pinToggle.setSelected(true);
        pinToggle.setToolTipText("Keep this console always on top of other windows");
        pinToggle.addActionListener(e -> applyPinState());
        applyPinState();

        tradingToggle = new JToggleButton("Trading OFF");
        tradingToggle.addActionListener(e -> {
            if (!updatingUi) {
                LvnStrategyState.setTradingEnabled(tradingToggle.isSelected(), "panel");
            }
        });

        longsToggle = new JToggleButton("Longs");
        shortsToggle = new JToggleButton("Shorts");
        longsToggle.addActionListener(e -> {
            if (!updatingUi) {
                LvnStrategyState.setSides(longsToggle.isSelected(), LvnStrategyState.allowShorts(), "panel");
            }
        });
        shortsToggle.addActionListener(e -> {
            if (!updatingUi) {
                LvnStrategyState.setSides(LvnStrategyState.allowLongs(), shortsToggle.isSelected(), "panel");
            }
        });

        JButton bothBtn = new JButton("Both");
        bothBtn.addActionListener(e -> LvnStrategyState.setSides(true, true, "panel Both"));
        JButton offBtn = new JButton("Off");
        offBtn.addActionListener(e -> LvnStrategyState.setSides(false, false, "panel Off"));

        JButton loadEsBtn = new JButton("Load ES chart export…");
        loadEsBtn.setToolTipText(
                "IRT multi-day chart export (or date,lvn2,lvn3 CSV). "
                        + "LVN2/LVN3 resolve as-of the replay/live session date at/before 14:30 UK.");
        loadEsBtn.addActionListener(e -> loadFile("ES"));
        JButton loadNqBtn = new JButton("Load NQ chart export…");
        loadNqBtn.setToolTipText(
                "IRT multi-day chart export (or date,lvn2,lvn3 CSV). "
                        + "Resolves to the current session/replay day.");
        loadNqBtn.addActionListener(e -> loadFile("NQ"));

        showChartToggle = new JToggleButton("Show LVNs on chart");
        showChartToggle.setSelected(true);
        showChartToggle.setToolTipText(
                "Plot loaded LVN2/LVN3 on the attached chart. "
                        + "Attach the add-on on ES and on NQ to see both markets' levels.");
        showChartToggle.addActionListener(e -> {
            if (!updatingUi) {
                LvnStrategyState.setShowLvnsOnChart(showChartToggle.isSelected(), "panel");
            }
        });

        showHvnToggle = new JToggleButton("Show HVNs on chart");
        showHvnToggle.setSelected(false);
        showHvnToggle.setToolTipText(
                "Plot HVN1/HVN2/HVN3 from the same IRT export row as the LVNs (as-of session @≤14:30). "
                        + "Requires HVN columns in the chart export.");
        showHvnToggle.addActionListener(e -> {
            if (!updatingUi) {
                LvnStrategyState.setShowHvnsOnChart(showHvnToggle.isSelected(), "panel");
            }
        });

        JButton applyBtn = new JButton("Apply settings");
        applyBtn.addActionListener(e -> applySettings());

        JButton flatten = new JButton("FLATTEN");
        flatten.setForeground(Color.WHITE);
        flatten.setBackground(new Color(0xB7, 0x1C, 0x1C));
        flatten.setOpaque(true);
        flatten.setBorderPainted(false);
        flatten.setFont(flatten.getFont().deriveFont(Font.BOLD, 13f));
        flatten.addActionListener(e -> LvnStrategyState.flattenAll());

        JButton cancelPending = new JButton("Cancel limits");
        cancelPending.addActionListener(e -> LvnStrategyState.cancelAllPending());

        JButton resetDay = new JButton("Reset day");
        resetDay.addActionListener(e -> {
            int ok = JOptionPane.showConfirmDialog(frame,
                    "Reset today's snapshot/arms and re-snap at next 14:30?\n"
                            + "Does not clear loaded LVNs.",
                    "Reset day", JOptionPane.OK_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE);
            if (ok == JOptionPane.OK_OPTION) {
                LvnStrategyState.resetDay("panel");
            }
        });

        LvnStrategyState.Settings s0 = LvnStrategyState.settings();
        slField = field(fmt(s0.slPts));
        tpField = field(fmt(s0.tpPts));
        offsetField = field(fmt(s0.offsetPts));
        nearField = field(fmt(s0.nearLvnPts));
        minOpenField = field(fmt(s0.minOpenDistPts));
        sizeField = field(Integer.toString(s0.orderSize));
        entryUntilField = field(minToHhmm(s0.entryUntilUkMin));
        eosField = field(minToHhmm(s0.eosUkMin));

        JPanel settings = new JPanel(new GridBagLayout());
        settings.setBorder(BorderFactory.createTitledBorder("Brackets / gates (Apply for new orders)"));
        GridBagConstraints gc = new GridBagConstraints();
        gc.insets = new Insets(2, 4, 2, 4);
        gc.fill = GridBagConstraints.HORIZONTAL;
        addField(settings, gc, 0, "SL pts", slField);
        addField(settings, gc, 1, "TP pts", tpField);
        addField(settings, gc, 2, "Offset", offsetField);
        addField(settings, gc, 3, "Near LVN", nearField);
        addField(settings, gc, 4, "Min open dist", minOpenField);
        addField(settings, gc, 5, "Size", sizeField);
        addField(settings, gc, 6, "Entry until UK", entryUntilField);
        addField(settings, gc, 7, "EOS UK", eosField);

        JPanel controls = new JPanel(new FlowLayout(FlowLayout.CENTER, 6, 4));
        controls.add(pinToggle);
        controls.add(loadEsBtn);
        controls.add(loadNqBtn);
        controls.add(showChartToggle);
        controls.add(showHvnToggle);
        controls.add(tradingToggle);
        controls.add(longsToggle);
        controls.add(shortsToggle);
        controls.add(bothBtn);
        controls.add(offBtn);
        controls.add(applyBtn);
        controls.add(cancelPending);
        controls.add(resetDay);
        controls.add(flatten);

        lvnModel = new DefaultTableModel(new Object[]{"Mkt", "ID", "Price", "Side", "Dist mid"}, 0) {
            @Override
            public boolean isCellEditable(int r, int c) {
                return false;
            }
        };
        JTable lvnTable = new JTable(lvnModel);
        lvnTable.setPreferredScrollableViewportSize(new Dimension(420, 140));

        ordersModel = new DefaultTableModel(
                new Object[]{"Side", "Centre", "Level", "Size", "Dist mid", "LVN dist", "State", "Conf"}, 0) {
            @Override
            public boolean isCellEditable(int r, int c) {
                return false;
            }
        };
        JTable ordersTable = new JTable(ordersModel);
        ordersTable.setPreferredScrollableViewportSize(new Dimension(520, 140));

        JPanel tables = new JPanel(new GridLayout(2, 1, 4, 4));
        tables.add(wrap("Levels loaded (LVN / HVN)", lvnTable));
        tables.add(wrap("Large orders in play", ordersTable));

        JPanel north = new JPanel(new BorderLayout(0, 2));
        north.add(statusLabel, BorderLayout.NORTH);
        JPanel midN = new JPanel(new BorderLayout());
        midN.add(levelsLabel, BorderLayout.NORTH);
        midN.add(warnLabel, BorderLayout.CENTER);
        midN.add(eventLabel, BorderLayout.SOUTH);
        north.add(midN, BorderLayout.CENTER);

        JPanel center = new JPanel(new BorderLayout(0, 6));
        center.setBorder(BorderFactory.createEmptyBorder(4, 8, 8, 8));
        center.add(controls, BorderLayout.NORTH);
        center.add(settings, BorderLayout.CENTER);
        center.add(tables, BorderLayout.SOUTH);

        frame.getContentPane().setLayout(new BorderLayout());
        frame.getContentPane().add(north, BorderLayout.NORTH);
        frame.getContentPane().add(center, BorderLayout.CENTER);

        listener = snap -> SwingUtilities.invokeLater(() -> refreshUi(snap));
        LvnStrategyState.addListener(listener);
        refreshUi(LvnStrategyState.snapshot());

        frame.pack();
        centerOnScreen();
    }

    private static JTextField field(String v) {
        JTextField f = new JTextField(v, 6);
        f.setHorizontalAlignment(SwingConstants.RIGHT);
        return f;
    }

    private static void addField(JPanel p, GridBagConstraints gc, int col, String label, JTextField field) {
        gc.gridx = col;
        gc.gridy = 0;
        gc.weightx = 0;
        p.add(new JLabel(label), gc);
        gc.gridy = 1;
        gc.weightx = 1;
        p.add(field, gc);
    }

    private static JPanel wrap(String title, JTable table) {
        JPanel p = new JPanel(new BorderLayout());
        p.setBorder(BorderFactory.createTitledBorder(title));
        p.add(new JScrollPane(table), BorderLayout.CENTER);
        return p;
    }

    private void loadFile(String market) {
        JFileChooser fc = new JFileChooser(lastDir);
        fc.setDialogTitle("Load " + market + " IRT chart export (LVN2/LVN3 as-of session day)");
        fc.setFileFilter(new FileNameExtensionFilter("CSV files", "csv", "CSV", "txt"));
        int res = fc.showOpenDialog(frame);
        if (res != JFileChooser.APPROVE_OPTION) {
            return;
        }
        File f = fc.getSelectedFile();
        lastDir = f.getParentFile() != null ? f.getParentFile() : lastDir;
        Path path = f.toPath();
        LocalDate session = LvnStrategyState.sessionDate();
        try {
            // Bind path; resolve LVN2/LVN3 for current replay/live session date @≤14:30
            LvnStrategyState.bindChartExport(market, path, "panel");
            LvnLevelFile.Levels lv = LvnStrategyState.levelsFor(market);
            if (lv == null || !lv.hasAny()) {
                JOptionPane.showMessageDialog(frame,
                        "No non-zero LVN2/LVN3 for session " + session + " in this file.\n"
                                + "For replay: start the feed first (so session date is known), "
                                + "or ensure the export covers that day.",
                        "LVN load", JOptionPane.ERROR_MESSAGE);
            }
        } catch (Exception ex) {
            Log.info("LVN load failed: " + ex);
            JOptionPane.showMessageDialog(frame, "Failed to load " + market + ":\n" + ex.getMessage(),
                    "LVN load", JOptionPane.ERROR_MESSAGE);
        }
    }

    private void applySettings() {
        try {
            LvnStrategyState.Settings s = LvnStrategyState.settings();
            s.slPts = Double.parseDouble(slField.getText().trim());
            s.tpPts = Double.parseDouble(tpField.getText().trim());
            s.offsetPts = Double.parseDouble(offsetField.getText().trim());
            s.nearLvnPts = Double.parseDouble(nearField.getText().trim());
            s.minOpenDistPts = Double.parseDouble(minOpenField.getText().trim());
            s.orderSize = Integer.parseInt(sizeField.getText().trim());
            s.entryUntilUkMin = hhmmToMin(entryUntilField.getText().trim());
            s.eosUkMin = hhmmToMin(eosField.getText().trim());
            LvnStrategyState.applySettings(s, "panel Apply");
        } catch (Exception ex) {
            JOptionPane.showMessageDialog(frame, "Invalid settings: " + ex.getMessage(),
                    "Apply", JOptionPane.ERROR_MESSAGE);
        }
    }

    private void refreshUi(LvnStrategyState.Snapshot snap) {
        updatingUi = true;
        try {
            statusLabel.setText(snap.status.name().replace('_', ' '));
            switch (snap.status) {
                case NO_LEVELS -> {
                    statusLabel.setBackground(new Color(0x61, 0x61, 0x61));
                    statusLabel.setForeground(Color.WHITE);
                }
                case LEVELS_LOADED -> {
                    statusLabel.setBackground(new Color(0x15, 0x65, 0xC0));
                    statusLabel.setForeground(Color.WHITE);
                }
                case ARMED -> {
                    statusLabel.setBackground(new Color(0x2E, 0x7D, 0x32));
                    statusLabel.setForeground(Color.WHITE);
                }
                case IN_POSITION -> {
                    statusLabel.setBackground(new Color(0xF9, 0xA8, 0x25));
                    statusLabel.setForeground(Color.BLACK);
                }
                case HALTED -> {
                    statusLabel.setBackground(new Color(0xB7, 0x1C, 0x1C));
                    statusLabel.setForeground(Color.WHITE);
                }
            }
            levelsLabel.setText(snap.statusDetail == null ? "" : snap.statusDetail
                    + (snap.midPts > 0 ? String.format(Locale.US, "   mid=%.2f", snap.midPts) : "")
                    + (snap.position != 0 ? "   pos=" + snap.position : ""));
            boolean bad = snap.stale || (snap.warning != null && !snap.warning.isEmpty());
            if (snap.warning != null && !snap.warning.isEmpty()) {
                warnLabel.setText(snap.warning);
            } else if (snap.stale) {
                warnLabel.setText("LVN date ≠ session " + snap.sessionDate);
            } else {
                String attach = (snap.esAttached ? "ES" : "")
                        + (snap.esAttached && snap.nqAttached ? "+" : "")
                        + (snap.nqAttached ? "NQ" : "");
                if (attach.isEmpty()) {
                    attach = "none";
                }
                warnLabel.setText(snap.sessionDate != null
                        ? ("As-of session " + snap.sessionDate + " @≤14:30 UK · attach=" + attach
                        + (snap.liveBookMarket.isEmpty() ? "" : " · book=" + snap.liveBookMarket))
                        : " ");
            }
            warnLabel.setForeground(bad
                    ? new Color(0xB7, 0x1C, 0x1C)
                    : new Color(0x2E, 0x7D, 0x32));
            eventLabel.setText(snap.lastEvent == null ? " " : snap.lastEvent);

            tradingToggle.setSelected(snap.tradingEnabled);
            tradingToggle.setText(snap.tradingEnabled ? "Trading ON" : "Trading OFF");
            showChartToggle.setSelected(snap.showLvnsOnChart);
            showChartToggle.setText(snap.showLvnsOnChart ? "Show LVNs on chart ON" : "Show LVNs on chart OFF");
            showHvnToggle.setSelected(snap.showHvnsOnChart);
            showHvnToggle.setText(snap.showHvnsOnChart ? "Show HVNs on chart ON" : "Show HVNs on chart OFF");
            longsToggle.setSelected(snap.allowLongs);
            shortsToggle.setSelected(snap.allowShorts);

            LvnStrategyState.Settings s = snap.settings;
            if (!slField.isFocusOwner()) {
                slField.setText(fmt(s.slPts));
            }
            if (!tpField.isFocusOwner()) {
                tpField.setText(fmt(s.tpPts));
            }
            if (!offsetField.isFocusOwner()) {
                offsetField.setText(fmt(s.offsetPts));
            }
            if (!nearField.isFocusOwner()) {
                nearField.setText(fmt(s.nearLvnPts));
            }
            if (!minOpenField.isFocusOwner()) {
                minOpenField.setText(fmt(s.minOpenDistPts));
            }
            if (!sizeField.isFocusOwner()) {
                sizeField.setText(Integer.toString(s.orderSize));
            }
            if (!entryUntilField.isFocusOwner()) {
                entryUntilField.setText(minToHhmm(s.entryUntilUkMin));
            }
            if (!eosField.isFocusOwner()) {
                eosField.setText(minToHhmm(s.eosUkMin));
            }

            lvnModel.setRowCount(0);
            double esMid = "ES".equals(snap.liveBookMarket) ? snap.midPts : Double.NaN;
            double nqMid = "NQ".equals(snap.liveBookMarket) ? snap.midPts : Double.NaN;
            if (snap.esLevels != null) {
                addLvnRow("ES", "LVN2", snap.esLevels.lvn2, esMid);
                addLvnRow("ES", "LVN3", snap.esLevels.lvn3, esMid);
                addLvnRow("ES", "HVN1", snap.esLevels.hvn1, esMid);
                addLvnRow("ES", "HVN2", snap.esLevels.hvn2, esMid);
                addLvnRow("ES", "HVN3", snap.esLevels.hvn3, esMid);
            }
            if (snap.nqLevels != null) {
                addLvnRow("NQ", "LVN2", snap.nqLevels.lvn2, nqMid);
                addLvnRow("NQ", "LVN3", snap.nqLevels.lvn3, nqMid);
                addLvnRow("NQ", "HVN1", snap.nqLevels.hvn1, nqMid);
                addLvnRow("NQ", "HVN2", snap.nqLevels.hvn2, nqMid);
                addLvnRow("NQ", "HVN3", snap.nqLevels.hvn3, nqMid);
            }

            ordersModel.setRowCount(0);
            if (snap.rows != null) {
                for (DisplayRow r : snap.rows) {
                    ordersModel.addRow(new Object[]{
                            r.side.name(),
                            fmt(r.centrePts),
                            fmt(r.levelPts),
                            r.size,
                            fmt(r.distMidPts),
                            Double.isNaN(r.lvnDist) ? "—" : fmt(r.lvnDist),
                            r.state.name(),
                            r.confluence ? "Y" : ""
                    });
                }
            }
        } finally {
            updatingUi = false;
        }
    }

    private void addLvnRow(String market, String id, double px, double mid) {
        if (px == 0.0) {
            return;
        }
        boolean haveMid = mid > 0 && !Double.isNaN(mid);
        String side = haveMid ? (px >= mid ? "above" : "below") : "—";
        String dist = haveMid ? fmt(Math.abs(px - mid)) : "—";
        lvnModel.addRow(new Object[]{market, id, fmt(px), side, dist});
    }

    private static String fmt(double v) {
        return String.format(Locale.US, "%.2f", v);
    }

    private static String minToHhmm(int minOfDay) {
        int h = Math.floorDiv(minOfDay, 60);
        int m = Math.floorMod(minOfDay, 60);
        return String.format(Locale.US, "%02d:%02d", h, m);
    }

    private static int hhmmToMin(String hhmm) {
        String[] p = hhmm.split(":");
        int h = Integer.parseInt(p[0].trim());
        int m = p.length > 1 ? Integer.parseInt(p[1].trim()) : 0;
        return h * 60 + m;
    }

    private static File defaultExportDir() {
        File irt = new File("C:\\Work\\Mat\\bookmap\\data\\irt");
        if (irt.isDirectory()) {
            return irt;
        }
        return new File(System.getProperty("user.home"));
    }

    private void centerOnScreen() {
        Dimension screen = Toolkit.getDefaultToolkit().getScreenSize();
        Dimension size = frame.getSize();
        int x = Math.max(0, (screen.width - size.width) / 2);
        int y = Math.max(0, (screen.height - size.height) / 4);
        frame.setLocation(x, y);
    }

    private void applyPinState() {
        boolean pinned = pinToggle.isSelected();
        frame.setAlwaysOnTop(pinned);
        pinToggle.setText(pinned ? "Pin ON" : "Pin OFF");
        pinToggle.setToolTipText(pinned
                ? "Pinned — console stays on top. Click to unpin."
                : "Unpinned — click to keep console always on top.");
    }

    private void showNow() {
        applyPinState();
        frame.setVisible(true);
        frame.toFront();
        Log.info("LVN Confluence panel shown");
    }

    public static void showPanel() {
        Runnable r = () -> {
            try {
                if (instance == null) {
                    instance = new LvnControlPanel();
                }
                instance.showNow();
            } catch (Throwable t) {
                Log.info("LVN panel FAILED: " + t);
                t.printStackTrace();
            }
        };
        if (SwingUtilities.isEventDispatchThread()) {
            r.run();
        } else {
            SwingUtilities.invokeLater(r);
        }
    }

    public static void hidePanel() {
        SwingUtilities.invokeLater(() -> {
            if (instance != null) {
                instance.frame.setVisible(false);
            }
        });
    }

    public static void disposePanel() {
        SwingUtilities.invokeLater(() -> {
            if (instance == null) {
                return;
            }
            LvnStrategyState.removeListener(instance.listener);
            instance.frame.dispose();
            instance = null;
            Log.info("LVN Confluence panel disposed");
        });
    }
}
