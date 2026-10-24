package com.bookmap.orderflow.trade;

import java.awt.KeyEventDispatcher;
import java.awt.KeyboardFocusManager;
import java.awt.event.KeyEvent;
import java.util.concurrent.atomic.AtomicInteger;

import velox.api.layer1.common.Log;

/**
 * Global Ctrl+Shift side-filter hotkeys for Momentum AutoTrade.
 * <p>
 * Avoids Ctrl+Alt (Windows / Evernote / Snipping Tool grab those). Uses
 * {@link KeyEventDispatcher} so toggles work as soon as MAT is attached.
 * <p>
 * <ul>
 *   <li>Ctrl+Shift+L — toggle allow longs</li>
 *   <li>Ctrl+Shift+K — toggle allow shorts</li>
 *   <li>Ctrl+Shift+B — both on</li>
 *   <li>Ctrl+Shift+X — both off (signals off)</li>
 * </ul>
 */
public final class MatSideHotkeys {

    private static final AtomicInteger REFS = new AtomicInteger();
    private static KeyEventDispatcher dispatcher;

    private MatSideHotkeys() {}

    public static synchronized void install() {
        int prev = REFS.getAndIncrement();
        // Always (re)show console when MAT attaches — does not require MAT Hotkeys v4 alone.
        MatControlPanel.showPanel();
        if (prev > 0) {
            return;
        }
        dispatcher = e -> {
            if (e.getID() != KeyEvent.KEY_PRESSED) {
                return false;
            }
            if (!(e.isControlDown() && e.isShiftDown()) || e.isAltDown()) {
                return false;
            }
            switch (e.getKeyCode()) {
                case KeyEvent.VK_L:
                    MatSideFilter.toggleLongs();
                    return true;
                case KeyEvent.VK_K:
                    MatSideFilter.toggleShorts();
                    return true;
                case KeyEvent.VK_B:
                    MatSideFilter.set(true, true, "hotkey Ctrl+Shift+B");
                    return true;
                case KeyEvent.VK_X:
                    MatSideFilter.set(false, false, "hotkey Ctrl+Shift+X");
                    return true;
                default:
                    return false;
            }
        };
        KeyboardFocusManager.getCurrentKeyboardFocusManager().addKeyEventDispatcher(dispatcher);
        Log.info("MAT side hotkeys ON: Ctrl+Shift+L toggle longs, Ctrl+Shift+K toggle shorts, "
                + "Ctrl+Shift+B both, Ctrl+Shift+X none");
    }

    public static synchronized void uninstall() {
        int left = REFS.decrementAndGet();
        if (left > 0) {
            return;
        }
        if (left < 0) {
            REFS.set(0);
            return;
        }
        if (dispatcher != null) {
            KeyboardFocusManager.getCurrentKeyboardFocusManager().removeKeyEventDispatcher(dispatcher);
            dispatcher = null;
            Log.info("MAT side hotkeys OFF");
        }
        // Last MAT instance gone — hide console (Hotkeys v4 may still re-show if loaded).
        MatControlPanel.hidePanel();
    }
}
