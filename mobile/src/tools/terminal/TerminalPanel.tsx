// Interaction adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-terminal/LICENSE.
import React, {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  ActivityIndicator,
  Alert,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import {
  KeyboardController,
  KeyboardEvents,
  KeyboardStickyView,
  useKeyboardState,
} from "react-native-keyboard-controller";
import * as Clipboard from "expo-clipboard";
import { ChevronDown, Ellipsis, Keyboard } from "lucide-react-native";
import { Button, IconButton } from "../../ui/controls";
import { NativeMenu } from "../../ui/NativeMenu";
import { useTheme } from "../../ui/theme";
import type { WorkspaceToolProps } from "../types";
import { hasNativeTerminalSurface } from "./nativeTerminalModule";
import {
  resolveModifiedTerminalInput,
  type PendingModifier,
} from "./terminalInput";
import { createTerminalPasteSession } from "./terminalPaste";
import { getTerminalSession } from "./terminalSession";
import { TerminalSurface, type TerminalSurfaceHandle } from "./TerminalSurface";
import { terminalThemeFor } from "./terminalTheme";

export const DEFAULT_TERMINAL_FONT_SIZE = 10.5;
export const TERMINAL_FONT_SIZE_STEP = 0.5;
export const MIN_TERMINAL_FONT_SIZE = 6;
export const MAX_TERMINAL_FONT_SIZE = 14;
const ACCESSORY_HEIGHT = 52;

/** Text size for this app session (shared by every workspace terminal). */
let terminalFontSize = DEFAULT_TERMINAL_FONT_SIZE;

type ToolbarAction =
  | { kind: "send"; key: string; label: string; data: string }
  | { kind: "clear"; key: string; label: string }
  | { kind: "paste"; key: string; label: string }
  | { kind: "modifier"; key: string; label: string; modifier: PendingModifier };

/** Keys a phone keyboard lacks; workspaces are Linux hosts (ctrl/alt). */
const TOOLBAR: ToolbarAction[] = [
  { kind: "send", key: "up", label: "↑", data: "\u001b[A" },
  { kind: "send", key: "down", label: "↓", data: "\u001b[B" },
  { kind: "send", key: "left", label: "←", data: "\u001b[D" },
  { kind: "send", key: "right", label: "→", data: "\u001b[C" },
  { kind: "send", key: "esc", label: "esc", data: "\u001b" },
  { kind: "modifier", key: "ctrl", label: "ctrl", modifier: "ctrl" },
  { kind: "modifier", key: "alt", label: "alt", modifier: "meta" },
  { kind: "send", key: "tab", label: "tab", data: "\t" },
  { kind: "paste", key: "paste", label: "paste" },
  { kind: "clear", key: "clear", label: "clear" },
  { kind: "send", key: "tilde", label: "~", data: "~" },
  { kind: "send", key: "pipe", label: "|", data: "|" },
  { kind: "send", key: "slash", label: "/", data: "/" },
  { kind: "send", key: "dash", label: "-", data: "-" },
];

// `useKeyboardState` flips on willShow/didHide; the accessory must leave as
// soon as the keyboard starts hiding, so it follows the will-events.
let keyboardShowing = KeyboardController.isVisible();
function subscribeKeyboardShowing(onChange: () => void) {
  keyboardShowing = KeyboardController.isVisible();
  const willShow = KeyboardEvents.addListener("keyboardWillShow", () => {
    keyboardShowing = true;
    onChange();
  });
  const willHide = KeyboardEvents.addListener("keyboardWillHide", () => {
    keyboardShowing = false;
    onChange();
  });
  return () => {
    willShow.remove();
    willHide.remove();
  };
}
const getKeyboardShowing = () => keyboardShowing;

const STATUS_LABELS = {
  idle: "Not started",
  starting: "Starting…",
  running: "Running",
  ended: "Ended",
  error: "Unavailable",
} as const;

/**
 * Workspace shell with the native libghostty surface, a key accessory above
 * the keyboard (arrows, esc, ctrl/alt, tab, paste, clear) and a show-keyboard
 * button. The shell outlives the sheet; options sit in the panel's menu.
 */
export function TerminalPanel({
  client,
  workspaceId,
  active,
  bottomInset,
  onInsertIntoChat,
}: WorkspaceToolProps): React.JSX.Element {
  const { theme } = useTheme();
  const terminalTheme = useMemo(() => terminalThemeFor(theme), [theme]);
  const session = useMemo(
    () => getTerminalSession(client, workspaceId),
    [client, workspaceId],
  );
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const isRunning = state.status === "running";
  const [fontSize, setFontSize] = useState(terminalFontSize);
  const [pendingModifier, setPendingModifier] =
    useState<PendingModifier | null>(null);
  const [keyboardFocusRequest, setKeyboardFocusRequest] = useState(0);
  const [captureRequest, setCaptureRequest] = useState(0);
  const captureSequence = useRef(0);
  const [pasteSession] = useState(createTerminalPasteSession);
  const surfaceRef = useRef<TerminalSurfaceHandle>(null);

  useEffect(() => {
    if (active) session.start();
  }, [active, session]);
  useEffect(() => {
    pasteSession.reset(isRunning);
  }, [isRunning, pasteSession, state.terminalId]);
  useEffect(() => {
    if (!active) void KeyboardController.dismiss().catch(() => undefined);
  }, [active]);

  const keyboard = useKeyboardState((value) => ({
    height: value.height,
    isVisible: value.isVisible,
  }));
  const showing = useSyncExternalStore(
    subscribeKeyboardShowing,
    getKeyboardShowing,
  );
  // The dismiss and show buttons flip the accessory ahead of the keyboard.
  const [accessory, setAccessory] = useState({ showing, dismissed: false });
  if (accessory.showing !== showing)
    setAccessory({ showing, dismissed: !showing });
  const accessoryVisible =
    active && isRunning && keyboard.isVisible && !accessory.dismissed;
  const keyboardOpen = active && keyboard.isVisible;
  const surfaceBottom =
    (keyboardOpen ? keyboard.height : 0) +
    (accessoryVisible ? ACCESSORY_HEIGHT : 0);

  const write = async (data: string) => session.write(data);
  const paste = () =>
    pasteSession.paste({
      readText: Clipboard.getStringAsync,
      write,
      onReadError: () => undefined,
    });
  const writeModified = (data: string) => {
    if (pendingModifier === null) {
      session.write(data);
      return;
    }
    setPendingModifier(null);
    const resolved = resolveModifiedTerminalInput({
      data,
      modifier: pendingModifier,
      hostPlatform: "linux",
    });
    if (resolved.kind === "paste") void paste();
    else session.write(resolved.data);
  };
  const onToolbarAction = (action: ToolbarAction) => {
    if (action.kind === "modifier") {
      setPendingModifier((current) =>
        current === action.modifier ? null : action.modifier,
      );
    } else if (action.kind === "clear") {
      setPendingModifier(null);
      session.clear();
    } else if (action.kind === "paste") {
      setPendingModifier(null);
      void paste();
    } else writeModified(action.data);
  };
  const showKeyboard = () => {
    if (!isRunning) return;
    setAccessory((current) => ({ ...current, dismissed: false }));
    setKeyboardFocusRequest((current) => current + 1);
    surfaceRef.current?.focusKeyboard();
  };
  const dismissKeyboard = () => {
    setAccessory((current) => ({ ...current, dismissed: true }));
    void KeyboardController.dismiss().catch(() => undefined);
  };
  const changeFontSize = (direction: 1 | -1) => {
    const next = Math.min(
      MAX_TERMINAL_FONT_SIZE,
      Math.max(
        MIN_TERMINAL_FONT_SIZE,
        fontSize + direction * TERMINAL_FONT_SIZE_STEP,
      ),
    );
    terminalFontSize = next;
    setFontSize(next);
  };
  const canAttach = Boolean(onInsertIntoChat) && hasNativeTerminalSurface();
  const menuActions = [
    {
      id: "font-decrease",
      title: `Smaller text (${Math.max(MIN_TERMINAL_FONT_SIZE, fontSize - TERMINAL_FONT_SIZE_STEP).toFixed(1)} pt)`,
      symbol: "textformat.size.smaller",
      disabled: fontSize <= MIN_TERMINAL_FONT_SIZE,
    },
    {
      id: "font-increase",
      title: `Larger text (${Math.min(MAX_TERMINAL_FONT_SIZE, fontSize + TERMINAL_FONT_SIZE_STEP).toFixed(1)} pt)`,
      symbol: "textformat.size.larger",
      disabled: fontSize >= MAX_TERMINAL_FONT_SIZE,
    },
    ...(canAttach
      ? [
          {
            id: "attach",
            title: "Add visible output to chat",
            symbol: "text.bubble",
            disabled: !isRunning,
          },
        ]
      : []),
    { id: "clear", title: "Clear", symbol: "eraser", disabled: !isRunning },
    {
      id: "restart",
      title: "Restart terminal",
      symbol: "arrow.clockwise",
      destructive: true,
    },
  ];
  const onMenu = (id: string) => {
    if (id === "font-decrease") changeFontSize(-1);
    else if (id === "font-increase") changeFontSize(1);
    else if (id === "clear") session.clear();
    else if (id === "restart") session.restart();
    else if (id === "attach") {
      void KeyboardController.dismiss().catch(() => undefined);
      captureSequence.current += 1;
      setCaptureRequest(captureSequence.current);
    }
  };

  const statusColor =
    state.status === "running"
      ? theme.success
      : state.status === "error"
        ? theme.danger
        : theme.subtleText;

  return (
    <View
      testID="terminal-panel"
      style={[styles.root, { backgroundColor: terminalTheme.background }]}
    >
      <View style={[styles.bar, { borderBottomColor: theme.border }]}>
        <View style={[styles.dot, { backgroundColor: statusColor }]} />
        <Text
          style={[styles.status, { color: theme.mutedText }]}
          numberOfLines={1}
          testID="terminal-status"
        >
          {STATUS_LABELS[state.status]}
        </Text>
        {state.status === "starting" ? (
          <ActivityIndicator size="small" color={theme.subtleText} />
        ) : null}
        <View style={styles.spacer} />
        <NativeMenu
          label="Terminal options"
          actions={menuActions}
          onSelect={onMenu}
          style={styles.menu}
          testID="terminal-menu"
        >
          <View style={styles.menuIcon}>
            <Ellipsis size={20} color={theme.text} />
          </View>
        </NativeMenu>
      </View>

      {state.status === "error" || (state.status === "idle" && !active) ? (
        <View style={styles.center}>
          <Text style={[styles.title, { color: theme.text }]}>
            {state.status === "error" ? "Terminal unavailable" : "No terminal"}
          </Text>
          {state.error ? (
            <Text style={[styles.detail, { color: theme.mutedText }]}>
              {state.error}
            </Text>
          ) : null}
          <Button
            title="Try again"
            variant="secondary"
            compact
            onPress={() => session.start()}
            style={{ alignSelf: "center" }}
            testID="terminal-retry"
          />
        </View>
      ) : (
        <View
          style={[
            styles.fill,
            {
              paddingBottom:
                surfaceBottom > 0
                  ? surfaceBottom
                  : Platform.OS === "android"
                    ? 0
                    : bottomInset,
            },
          ]}
        >
          <TerminalSurface
            ref={surfaceRef}
            session={session}
            terminalKey={`${workspaceId}:${state.generation}`}
            fontSize={fontSize}
            theme={terminalTheme}
            scheme={theme.scheme}
            isRunning={isRunning}
            autoFocus={false}
            keyboardFocusRequest={active ? keyboardFocusRequest : 0}
            captureRequest={captureRequest}
            onCapture={(text) => {
              setCaptureRequest(0);
              if (!text.trim()) {
                Alert.alert(
                  "No terminal output",
                  "There is no visible output to add.",
                );
                return;
              }
              onInsertIntoChat?.(`\`\`\`text\n${text.trimEnd()}\n\`\`\`\n`);
            }}
            onInput={writeModified}
            onResize={(grid) => session.resize(grid)}
            style={styles.fill}
          />
        </View>
      )}

      {state.status === "ended" ? (
        <View
          style={[
            styles.ended,
            {
              backgroundColor: theme.surfaceMuted,
              borderColor: theme.border,
              bottom: bottomInset + 16,
            },
          ]}
        >
          <Text style={[styles.status, { color: theme.text }]}>
            Shell ended
          </Text>
          <Button
            title="Start new shell"
            variant="secondary"
            compact
            onPress={() => session.start()}
            testID="terminal-start"
          />
        </View>
      ) : null}

      {accessoryVisible ? (
        <KeyboardStickyView
          style={styles.accessoryDock}
          offset={{ closed: 0, opened: 0 }}
        >
          <View
            style={[
              styles.accessory,
              {
                backgroundColor: terminalTheme.background,
                borderTopColor: theme.border,
              },
            ]}
            testID="terminal-accessory"
          >
            <ScrollView
              horizontal
              keyboardShouldPersistTaps="always"
              showsHorizontalScrollIndicator={false}
              contentContainerStyle={styles.keys}
            >
              {TOOLBAR.map((action) => {
                const selected =
                  action.kind === "modifier" &&
                  pendingModifier === action.modifier;
                return (
                  <Pressable
                    key={action.key}
                    accessibilityRole="button"
                    accessibilityLabel={action.label}
                    accessibilityState={{ selected }}
                    onPress={() => onToolbarAction(action)}
                    testID={`terminal-key-${action.key}`}
                    android_ripple={{
                      color: theme.controlFill,
                      borderless: false,
                    }}
                    style={({ pressed }) => [
                      styles.key,
                      {
                        minWidth: action.label.length > 1 ? 52 : 42,
                        backgroundColor: selected
                          ? theme.text
                          : pressed
                            ? theme.border
                            : theme.controlFill,
                      },
                    ]}
                  >
                    <Text
                      style={[
                        styles.keyLabel,
                        {
                          color: selected
                            ? theme.background
                            : terminalTheme.foreground,
                          textTransform:
                            action.kind === "modifier" ||
                            action.kind === "clear"
                              ? "uppercase"
                              : "none",
                        },
                      ]}
                    >
                      {action.label}
                    </Text>
                  </Pressable>
                );
              })}
            </ScrollView>
            <IconButton
              label="Dismiss keyboard"
              symbol="keyboard.chevron.compact.down"
              icon={<ChevronDown size={20} color={theme.text} />}
              tone="quiet"
              onPress={dismissKeyboard}
              testID="terminal-dismiss-keyboard"
            />
          </View>
        </KeyboardStickyView>
      ) : active && isRunning && !keyboard.isVisible ? (
        <View
          style={[styles.showKeyboard, { bottom: bottomInset + 16 }]}
          pointerEvents="box-none"
        >
          <IconButton
            label="Show keyboard"
            symbol="keyboard"
            icon={<Keyboard size={20} color={theme.text} />}
            tone="floating"
            size={52}
            onPress={showKeyboard}
            testID="terminal-show-keyboard"
          />
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  fill: { flex: 1 },
  bar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingLeft: 16,
    paddingRight: 6,
    minHeight: 44,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  status: { fontSize: 13, fontWeight: "500" },
  spacer: { flex: 1 },
  menu: { width: 44, height: 44 },
  menuIcon: {
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    paddingHorizontal: 32,
  },
  title: { fontSize: 17, fontWeight: "600" },
  detail: { fontSize: 14, textAlign: "center" },
  ended: {
    position: "absolute",
    alignSelf: "center",
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingLeft: 16,
    paddingRight: 6,
    paddingVertical: 4,
    borderRadius: 999,
    borderWidth: StyleSheet.hairlineWidth,
  },
  accessoryDock: { position: "absolute", left: 0, right: 0, bottom: 0 },
  accessory: {
    minHeight: ACCESSORY_HEIGHT,
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 8,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  keys: { gap: 6, alignItems: "center", paddingRight: 4 },
  key: {
    height: 36,
    borderRadius: 10,
    paddingHorizontal: 10,
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  keyLabel: {
    fontSize: 14,
    fontWeight: "600",
    fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
  },
  showKeyboard: { position: "absolute", right: 16 },
});
