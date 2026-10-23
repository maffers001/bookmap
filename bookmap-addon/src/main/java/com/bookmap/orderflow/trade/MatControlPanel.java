package com.bookmap.orderflow.trade;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.GridLayout;
import java.awt.Toolkit;
import java.awt.event.KeyEvent;
import java.util.function.BiConsumer;

import javax.swing.AbstractAction;
import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JComponent;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JToggleButton;
import javax.swing.KeyStroke;
import javax.swing.SwingConstants;
import javax.swing.SwingUtilities;
import javax.swing.WindowConstants;

import velox.api.layer1.common.Log;

/**
 * Always-on-top MAT side-filter console (MAT v3). Buttons and Ctrl+Shift L/K/B/X
 * when the window is focused; {@link MatSideHotkeys} still covers Bookmap focus.
 * Opens when MAT attaches or when {@code MAT Hotkeys v3} loads.
 */
public final class MatControlPanel {

    private static MatControlPanel instance;

    private final JFrame frame;
    private final JLabel statusLabel;
    private final JToggleButton toggleLongs;
    private final JToggleButton toggleShorts;
    private final BiConsumer<Boolean, Boolean> listener;
    private boolean updatingUi;

    private MatControlPanel() {
        frame = new JFrame("MAT Side v3");
        frame.setAlwaysOnTop(true);
        frame.setDefaultCloseOperation(WindowConstants.HIDE_ON_CLOSE);
        frame.setResizable(false);
        frame.setType(java.awt.Window.Type.UTILITY);

        statusLabel = new JLabel(MatSideFilter.label(), SwingConstants.CENTER);
        statusLabel.setFont(statusLabel.getFont().deriveFont(Font.BOLD, 22f));
        statusLabel.setOpaque(true);
        statusLabel.setBorder(BorderFactory.createEmptyBorder(12, 16, 12, 16));

        JPanel modes = new JPanel(new GridLayout(1, 4, 6, 0));
        modes.add(modeButton("Both", () -> MatSideFilter.set(true, true, "panel Both")));
        modes.add(modeButton("Longs", () -> MatSideFilter.set(true, false, "panel Longs")));
        modes.add(modeButton("Shorts", () -> MatSideFilter.set(false, true, "panel Shorts")));
        modes.add(modeButton("Off", () -> MatSideFilter.set(false, false, "panel Off")));

        toggleLongs = new JToggleButton("Longs");
        toggleShorts = new JToggleButton("Shorts");
        toggleLongs.addActionListener(e -> {
            if (!updatingUi) {
                MatSideFilter.set(toggleLongs.isSelected(), MatSideFilter.allowShorts(), "panel toggle longs");
            }
        });
        toggleShorts.addActionListener(e -> {
            if (!updatingUi) {
                MatSideFilter.set(MatSideFilter.allowLongs(), toggleShorts.isSelected(), "panel toggle shorts");
            }
        });
        JPanel toggles = new JPanel(new FlowLayout(FlowLayout.CENTER, 8, 4));
        toggles.add(toggleLongs);
        toggles.add(toggleShorts);

        JButton flatten = new JButton("FLATTEN");
        flatten.setForeground(Color.WHITE);
        flatten.setBackground(new Color(0xB7, 0x1C, 0x1C));
        flatten.setOpaque(true);
        flatten.setBorderPainted(false);
        flatten.setFont(flatten.getFont().deriveFont(Font.BOLD, 14f));
        flatten.setToolTipText("Set side filter to NONE and market-close open MAT position(s)");
        flatten.addActionListener(e -> MatTradeControl.flattenAll());
        JPanel flattenRow = new JPanel(new FlowLayout(FlowLayout.CENTER, 0, 4));
        flattenRow.add(flatten);

        JLabel footer = new JLabel(
                "<html><center>Ctrl+Shift+L/K toggle &nbsp; B both &nbsp; X off<br/>"
                        + "(works with panel or Bookmap focused)</center></html>",
                SwingConstants.CENTER);
        footer.setBorder(BorderFactory.createEmptyBorder(4, 8, 8, 8));

        JPanel mid = new JPanel(new BorderLayout(0, 4));
        mid.add(toggles, BorderLayout.NORTH);
        mid.add(flattenRow, BorderLayout.CENTER);

        JPanel center = new JPanel(new BorderLayout(0, 8));
        center.setBorder(BorderFactory.createEmptyBorder(8, 10, 4, 10));
        center.add(modes, BorderLayout.NORTH);
        center.add(mid, BorderLayout.CENTER);

        frame.getContentPane().setLayout(new BorderLayout());
        frame.getContentPane().add(statusLabel, BorderLayout.NORTH);
        frame.getContentPane().add(center, BorderLayout.CENTER);
        frame.getContentPane().add(footer, BorderLayout.SOUTH);

        installKeyBindings();
        refreshUi(MatSideFilter.allowLongs(), MatSideFilter.allowShorts());

        listener = (L, S) -> SwingUtilities.invokeLater(() -> refreshUi(L, S));
        MatSideFilter.addListener(listener);

        frame.pack();
        centerOnScreen();
    }

    private void centerOnScreen() {
        Dimension screen = Toolkit.getDefaultToolkit().getScreenSize();
        Dimension size = frame.getSize();
        int x = Math.max(0, (screen.width - size.width) / 2);
        int y = Math.max(0, (screen.height - size.height) / 3);
        frame.setLocation(x, y);
    }

    private static JButton modeButton(String text, Runnable action) {
        JButton b = new JButton(text);
        b.addActionListener(e -> action.run());
        return b;
    }

    private void installKeyBindings() {
        JComponent root = frame.getRootPane();
        var input = root.getInputMap(JComponent.WHEN_IN_FOCUSED_WINDOW);
        var actions = root.getActionMap();
        bind(input, actions, KeyEvent.VK_L, "mat.panel.toggleLongs", MatSideFilter::toggleLongs);
        bind(input, actions, KeyEvent.VK_K, "mat.panel.toggleShorts", MatSideFilter::toggleShorts);
        bind(input, actions, KeyEvent.VK_B, "mat.panel.both",
                () -> MatSideFilter.set(true, true, "panel Ctrl+Shift+B"));
        bind(input, actions, KeyEvent.VK_X, "mat.panel.none",
                () -> MatSideFilter.set(false, false, "panel Ctrl+Shift+X"));
    }

    private static void bind(
            javax.swing.InputMap input,
            javax.swing.ActionMap actions,
            int keyCode,
            String name,
            Runnable run) {
        input.put(KeyStroke.getKeyStroke(keyCode, KeyEvent.CTRL_DOWN_MASK | KeyEvent.SHIFT_DOWN_MASK), name);
        actions.put(name, new AbstractAction() {
            @Override
            public void actionPerformed(java.awt.event.ActionEvent e) {
                run.run();
            }
        });
    }

    private void refreshUi(boolean longs, boolean shorts) {
        updatingUi = true;
        try {
            statusLabel.setText(MatSideFilter.label());
            if (longs && shorts) {
                statusLabel.setBackground(new Color(0x2E, 0x7D, 0x32));
                statusLabel.setForeground(Color.WHITE);
            } else if (longs) {
                statusLabel.setBackground(new Color(0x15, 0x65, 0xC0));
                statusLabel.setForeground(Color.WHITE);
            } else if (shorts) {
                statusLabel.setBackground(new Color(0xC6, 0x28, 0x28)); // red — shorts only
                statusLabel.setForeground(Color.WHITE);
            } else {
                statusLabel.setBackground(new Color(0x61, 0x61, 0x61));
                statusLabel.setForeground(Color.WHITE);
            }
            toggleLongs.setSelected(longs);
            toggleShorts.setSelected(shorts);
        } finally {
            updatingUi = false;
        }
    }

    private void showNow() {
        frame.setAlwaysOnTop(true);
        frame.setVisible(true);
        frame.toFront();
        frame.requestFocus();
        Log.info("MAT Side v3 panel shown (visible=" + frame.isVisible()
                + " loc=" + frame.getX() + "," + frame.getY() + ")");
    }

    /** Show the singleton panel (EDT-safe). */
    public static void showPanel() {
        Runnable r = () -> {
            try {
                if (instance == null) {
                    instance = new MatControlPanel();
                }
                instance.showNow();
            } catch (Throwable t) {
                Log.info("MAT Side v3 panel FAILED to show: " + t);
                t.printStackTrace();
            }
        };
        if (SwingUtilities.isEventDispatchThread()) {
            r.run();
        } else {
            SwingUtilities.invokeLater(r);
        }
    }

    /** Hide without disposing (filter unchanged). */
    public static void hidePanel() {
        SwingUtilities.invokeLater(() -> {
            if (instance != null) {
                instance.frame.setVisible(false);
                Log.info("MAT Side v3 panel hidden");
            }
        });
    }

    /** Dispose and clear singleton (call when MAT Hotkeys v3 finishes). */
    public static void disposePanel() {
        SwingUtilities.invokeLater(() -> {
            if (instance == null) {
                return;
            }
            MatSideFilter.removeListener(instance.listener);
            instance.frame.dispose();
            instance = null;
            Log.info("MAT Side v3 panel disposed");
        });
    }
}
