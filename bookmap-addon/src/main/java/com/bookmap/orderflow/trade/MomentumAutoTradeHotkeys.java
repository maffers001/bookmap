package com.bookmap.orderflow.trade;

import java.awt.event.KeyEvent;
import java.util.HashSet;
import java.util.Set;

import velox.api.layer1.Layer1ApiFinishable;
import velox.api.layer1.Layer1ApiProvider;
import velox.api.layer1.actions.Layer1ActionMapper;
import velox.api.layer1.actions.Layer1ActionsContainer;
import velox.api.layer1.actions.Layer1ExternalAction;
import velox.api.layer1.actions.annotations.Layer1ActionMetadata;
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1Injectable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.common.Log;
import velox.api.layer1.layers.Layer1ApiInjectorRelay;

/**
 * Classic Layer1 companion so Bookmap registers keyboard shortcuts under
 * Settings → Keyboard Shortcuts → Addons / MAT Hotkeys v5.
 * <p>
 * Opens the always-on-top {@link MatControlPanel}. Simplified MAT modules do
 * not appear in Keyboard Shortcuts; enable this addon once (same JAR).
 * Hotkeys and the panel update {@link MatSideFilter}.
 */
@Layer1Injectable
@Layer1StrategyName("MAT Hotkeys v5")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class MomentumAutoTradeHotkeys extends Layer1ApiInjectorRelay
        implements Layer1ApiFinishable, Layer1ActionMapper {

    static final String ACTION_SIDE_BOTH = "mat.side.both";
    static final String ACTION_SIDE_LONGS = "mat.side.longs";
    static final String ACTION_SIDE_SHORTS = "mat.side.shorts";
    static final String ACTION_SIDE_NONE = "mat.side.none";
    static final String ACTION_TOGGLE_LONGS = "mat.toggle.longs";
    static final String ACTION_TOGGLE_SHORTS = "mat.toggle.shorts";
    static final String ACTION_SHOW_PANEL = "mat.panel.show";

    public MomentumAutoTradeHotkeys(Layer1ApiProvider provider) {
        super(provider);
        MatControlPanel.showPanel();
        Log.info("MAT Hotkeys v5 loaded (side panel + keyboard actions)");
    }

    @Override
    public void finish() {
        MatControlPanel.disposePanel();
        Log.info("MAT Hotkeys v5 finished");
    }

    @Override
    public Layer1ActionsContainer getActionsContainer() {
        Set<Layer1ExternalAction> actions = new HashSet<>();
        actions.add(new SideModeAction());
        actions.add(new ToggleLongsAction());
        actions.add(new ToggleShortsAction());
        actions.add(new ShowPanelAction());
        return new Layer1ActionsContainer(actions);
    }

    @Layer1ActionMetadata(id = ACTION_SIDE_BOTH, name = "Side: Both", groups = {"Side filter"})
    @Layer1ActionMetadata(id = ACTION_SIDE_LONGS, name = "Side: Longs only", groups = {"Side filter"})
    @Layer1ActionMetadata(id = ACTION_SIDE_SHORTS, name = "Side: Shorts only", groups = {"Side filter"})
    @Layer1ActionMetadata(id = ACTION_SIDE_NONE, name = "Side: None (signals off)", groups = {"Side filter"})
    public class SideModeAction implements Layer1ExternalAction {
        @Override
        public boolean performAction(String actionId, KeyEvent e) {
            if (ACTION_SIDE_BOTH.equals(actionId)) {
                MatSideFilter.set(true, true, actionId);
            } else if (ACTION_SIDE_LONGS.equals(actionId)) {
                MatSideFilter.set(true, false, actionId);
            } else if (ACTION_SIDE_SHORTS.equals(actionId)) {
                MatSideFilter.set(false, true, actionId);
            } else if (ACTION_SIDE_NONE.equals(actionId)) {
                MatSideFilter.set(false, false, actionId);
            } else {
                return false;
            }
            return true;
        }

        @Override
        public void onShortcutChanged(String actionId, Set<String> shortcuts) {
            Log.info("MAT hotkey bound " + actionId + " → " + shortcuts);
        }
    }

    @Layer1ActionMetadata(id = ACTION_TOGGLE_LONGS, name = "Toggle Allow longs", groups = {"Side filter"})
    public class ToggleLongsAction implements Layer1ExternalAction {
        @Override
        public boolean performAction(String actionId, KeyEvent e) {
            MatSideFilter.toggleLongs();
            return true;
        }

        @Override
        public void onShortcutChanged(String actionId, Set<String> shortcuts) {
            Log.info("MAT hotkey bound " + actionId + " → " + shortcuts);
        }
    }

    @Layer1ActionMetadata(id = ACTION_TOGGLE_SHORTS, name = "Toggle Allow shorts", groups = {"Side filter"})
    public class ToggleShortsAction implements Layer1ExternalAction {
        @Override
        public boolean performAction(String actionId, KeyEvent e) {
            MatSideFilter.toggleShorts();
            return true;
        }

        @Override
        public void onShortcutChanged(String actionId, Set<String> shortcuts) {
            Log.info("MAT hotkey bound " + actionId + " → " + shortcuts);
        }
    }

    @Layer1ActionMetadata(id = ACTION_SHOW_PANEL, name = "Show MAT Side panel", groups = {"Side filter"})
    public class ShowPanelAction implements Layer1ExternalAction {
        @Override
        public boolean performAction(String actionId, KeyEvent e) {
            MatControlPanel.showPanel();
            return true;
        }

        @Override
        public void onShortcutChanged(String actionId, Set<String> shortcuts) {
            Log.info("MAT hotkey bound " + actionId + " → " + shortcuts);
        }
    }
}
