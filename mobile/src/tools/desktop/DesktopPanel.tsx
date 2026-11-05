import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIndicator,
  AppState,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  useWindowDimensions,
  type LayoutChangeEvent,
} from "react-native";
import {
  KeyboardController,
  useKeyboardState,
} from "react-native-keyboard-controller";
import { runOnUI, withTiming } from "react-native-reanimated";
import * as Clipboard from "expo-clipboard";
import { Ellipsis, Keyboard, MousePointerClick } from "lucide-react-native";
import {
  DESKTOP_CONNECTION_TIMEOUT_MS,
  createDesktopReconnectBackoff,
  createPausableTimer,
  workspaceDesktopSize,
} from "@opencuria/chat-core/desktopSurface";
import { Button, IconButton } from "../../ui/controls";
import { NativeMenu, type NativeMenuAction } from "../../ui/NativeMenu";
import { lightTapHaptic } from "../../ui/haptics";
import { useTheme } from "../../ui/theme";
import { showToast } from "../../ui/toast";
import { KEY_ACCESSORY_HEIGHT, KeyAccessoryBar } from "../KeyAccessoryBar";
import type { WorkspaceToolProps } from "../types";
import {
  DesktopScreen,
  useDesktopViewport,
  type DesktopCursorImage,
  type DesktopScreenHandle,
} from "./DesktopScreen";
import { DesktopTrackpad, type MouseButton } from "./DesktopTrackpad";
import {
  DESKTOP_ACCESSORY_KEYS,
  DESKTOP_KEYS,
  KEYBOARD_SENTINEL,
  diffKeyboardInput,
  modifiersOf,
  type DesktopModifier,
  type KeyboardInputAction,
} from "./desktopKeys";
import { useDesktopViewer } from "./desktopSession";
import { clamp, fitBox, type Size } from "./pointerMath";
import {
  NO_MODIFIERS,
  keyCommand,
  parseDesktopViewerMessage,
  pointerCommand,
  scrollCommand,
  textCommand,
  type DesktopKey,
  type DesktopModifiers,
} from "./viewerBridge";

type ViewerStatus = "connecting" | "connected" | "disconnected";

const STATUS_BAR_HEIGHT = 44;
const GAP = 12;
const TRACKPAD_MIN_HEIGHT = 140;
const KEYBOARD_BUTTON_SIZE = 52;
/** Trackpad height kept while the keyboard covers the lower half. */
const TRACKPAD_MIN_KEYBOARD = 96;

/**
 * The workspace's KasmVNC desktop: the remote screen on top, a trackpad for
 * the pointer below and a keyboard button at the bottom right. The viewer
 * lease and recovery follow the webapp's shared rules; the desktop keeps
 * running while other sections of the tools sheet are shown.
 */
export function DesktopPanel({
  client,
  workspaceId,
  active,
  bottomInset,
}: WorkspaceToolProps): React.JSX.Element {
  const { theme } = useTheme();
  const dimensions = useWindowDimensions();
  const viewer = useDesktopViewer(client, workspaceId, active);
  const { coordinator, snapshot, computerUseActive } = viewer;

  // --- Framebuffer size: workspace setting until the viewer reports it ---
  const [fb, setFb] = useState<Size>(() => workspaceDesktopSize(null));
  useEffect(() => {
    const controller = new AbortController();
    client.workspaces
      .get(workspaceId, controller.signal)
      .then((workspace) => setFb(workspaceDesktopSize(workspace)))
      .catch(() => undefined);
    return () => controller.abort();
  }, [client, workspaceId]);
  const viewport = useDesktopViewport(fb);
  // JS mirror of the pointer: shared values written here are applied on the
  // UI thread later, so input is sent from this copy.
  const pointer = useRef({ x: fb.width / 2, y: fb.height / 2 });
  const setPointer = useCallback(
    (x: number, y: number) => {
      pointer.current = { x, y };
      viewport.pointerX.value = x;
      viewport.pointerY.value = y;
    },
    [viewport],
  );
  useEffect(() => {
    viewport.fb.value = fb;
    setPointer(
      clamp(pointer.current.x, 0, fb.width - 1),
      clamp(pointer.current.y, 0, fb.height - 1),
    );
  }, [fb, setPointer, viewport]);

  // --- Viewer page lifecycle (mirrors the webapp's DesktopSurface) ---
  const [generation, setGeneration] = useState(0);
  const [viewerStatus, setViewerStatus] = useState<ViewerStatus>("connecting");
  const [recoveryError, setRecoveryError] = useState(false);
  const [cursor, setCursor] = useState<DesktopCursorImage | null>(null);
  const viewerStatusRef = useRef<ViewerStatus>("connecting");
  // A new viewer waits for the held lease; a mounted one rides out outages.
  const mountedRoute = useRef<string | null>(null);
  const proxyUrl =
    snapshot.ready ||
    (mountedRoute.current !== null && snapshot.route === mountedRoute.current)
      ? snapshot.route
      : null;
  mountedRoute.current = proxyUrl;
  const proxyUrlRef = useRef(proxyUrl);
  proxyUrlRef.current = proxyUrl;
  const viewerKey = `${proxyUrl}:${snapshot.revision}:${generation}`;
  const url = useMemo(
    () =>
      proxyUrl
        ? client.desktop.viewerUrl(proxyUrl, {
            viewer_client_id: coordinator.viewerClientId,
            intent_revision: snapshot.revision,
          })
        : "",
    // The token is resolved together with the remount key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [viewerKey],
  );

  const setStatus = useCallback((status: ViewerStatus) => {
    viewerStatusRef.current = status;
    setViewerStatus(status);
  }, []);
  const recoveryPending = useRef(false);
  const mountedRef = useRef(true);
  useEffect(() => {
    // Effects may re-run (Fast Refresh, StrictMode); mark mounted each time.
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const recovery = useMemo(() => {
    const markDisconnected = () => {
      if (!proxyUrlRef.current) return;
      timeout.cancel();
      setStatus("disconnected");
      backoff.schedule();
      if (AppState.currentState !== "active") backoff.pause();
    };
    const timeout = createPausableTimer(() => {
      if (viewerStatusRef.current !== "connected") markDisconnected();
    });
    const startTimeout = () => {
      if (!timeout.pending) timeout.start(DESKTOP_CONNECTION_TIMEOUT_MS);
      if (AppState.currentState !== "active") timeout.pause();
    };
    const reconnect = async () => {
      if (recoveryPending.current) return;
      recoveryPending.current = true;
      try {
        // Reconcile the lease before loading another viewer page.
        const remount = await coordinator.reconnect(
          workspaceId,
          Boolean(proxyUrlRef.current),
        );
        if (remount && mountedRef.current) setGeneration((value) => value + 1);
      } finally {
        recoveryPending.current = false;
        if (mountedRef.current && viewerStatusRef.current !== "connected")
          startTimeout();
      }
    };
    const backoff = createDesktopReconnectBackoff(
      () => {
        if (AppState.currentState !== "active") return;
        setStatus("connecting");
        setRecoveryError(false);
        void reconnect();
      },
      () => setRecoveryError(true),
    );
    return { timeout, backoff, startTimeout, markDisconnected };
  }, [coordinator, setStatus, workspaceId]);

  useEffect(
    () => () => {
      recovery.timeout.cancel();
      recovery.backoff.reset();
    },
    [recovery],
  );

  useEffect(() => {
    setStatus("connecting");
    setCursor(null);
    recovery.timeout.cancel();
    if (url) recovery.startTimeout();
  }, [recovery, setStatus, url, viewerKey]);

  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") {
        recovery.timeout.resume();
        recovery.backoff.resume();
      } else {
        recovery.timeout.pause();
        recovery.backoff.pause();
      }
    });
    return () => subscription.remove();
  }, [recovery]);

  const screenRef = useRef<DesktopScreenHandle>(null);
  const inject = useCallback((script: string) => {
    screenRef.current?.inject(script);
  }, []);

  const markConnected = () => {
    setStatus("connected");
    setRecoveryError(false);
    recovery.timeout.cancel();
    recovery.backoff.reset();
    // Put the remote pointer where the native cursor is.
    inject(pointerCommand(pointer.current.x, pointer.current.y, 0));
  };

  const onMessage = (data: string) => {
    const message = parseDesktopViewerMessage(data);
    if (!message) return;
    if (message.type === "state") {
      if (message.state === "connected") {
        if (viewerStatusRef.current !== "connected") markConnected();
      } else if (message.state === "disconnected") {
        // Before the first connection the load timeout decides.
        if (viewerStatusRef.current === "connected")
          recovery.markDisconnected();
      } else if (viewerStatusRef.current !== "connected") {
        setStatus("connecting");
        recovery.startTimeout();
      }
    } else if (message.type === "framebuffer") {
      // The canvas only takes input once it has a size.
      inject(pointerCommand(pointer.current.x, pointer.current.y, 0));
      setFb((current) =>
        current.width === message.width && current.height === message.height
          ? current
          : { width: message.width, height: message.height },
      );
    } else if (message.type === "cursor") {
      setCursor(
        message.image
          ? {
              uri: message.image,
              width: message.width,
              height: message.height,
              hotX: message.hotX,
              hotY: message.hotY,
            }
          : null,
      );
    }
  };

  const retry = () => {
    if (!proxyUrl) {
      void coordinator.acquire(workspaceId);
      return;
    }
    recovery.timeout.cancel();
    setRecoveryError(false);
    setStatus("connecting");
    recovery.backoff.retryNow();
    recovery.startTimeout();
  };

  // --- Input ---
  const connected = Boolean(proxyUrl) && viewerStatus === "connected";
  const canControl = connected && !computerUseActive;
  const [modifiers, setModifiers] = useState<ReadonlySet<DesktopModifier>>(
    () => new Set(),
  );
  const modifiersRef = useRef(modifiers);
  modifiersRef.current = modifiers;
  const consumeModifiers = (): DesktopModifiers => {
    const current = modifiersRef.current;
    if (current.size === 0) return NO_MODIFIERS;
    modifiersRef.current = new Set();
    setModifiers(modifiersRef.current);
    return modifiersOf(current);
  };

  const buttons = useRef(0);
  const dragModifiers = useRef<DesktopModifiers>(NO_MODIFIERS);
  const pendingMove = useRef<{ x: number; y: number } | null>(null);
  const pendingScroll = useRef({ x: 0, y: 0 });
  const frame = useRef<number | null>(null);
  const flush = () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    const move = pendingMove.current;
    pendingMove.current = null;
    if (move)
      inject(
        pointerCommand(
          move.x,
          move.y,
          buttons.current,
          buttons.current ? dragModifiers.current : NO_MODIFIERS,
        ),
      );
    const scroll = pendingScroll.current;
    if (scroll.x || scroll.y) {
      pendingScroll.current = { x: 0, y: 0 };
      inject(
        scrollCommand(pointer.current.x, pointer.current.y, scroll.x, scroll.y),
      );
    }
  };
  const schedule = () => {
    if (frame.current === null) frame.current = requestAnimationFrame(flush);
  };
  const onMove = (x: number, y: number) => {
    pointer.current = { x, y };
    pendingMove.current = { x, y };
    schedule();
  };
  const onScroll = (deltaX: number, deltaY: number) => {
    pendingScroll.current = {
      x: pendingScroll.current.x + deltaX,
      y: pendingScroll.current.y + deltaY,
    };
    schedule();
  };
  const click = (button: MouseButton) => {
    flush();
    const mask = button === "left" ? 1 : 2;
    const { x, y } = pointer.current;
    const held = consumeModifiers();
    inject(pointerCommand(x, y, mask, held));
    inject(pointerCommand(x, y, 0, held));
    lightTapHaptic();
  };
  const drag = (down: boolean) => {
    flush();
    if (down) dragModifiers.current = consumeModifiers();
    buttons.current = down ? 1 : 0;
    inject(
      pointerCommand(
        pointer.current.x,
        pointer.current.y,
        buttons.current,
        dragModifiers.current,
      ),
    );
    if (down) lightTapHaptic();
    else dragModifiers.current = NO_MODIFIERS;
  };
  const tapAt = (x: number, y: number) => {
    setPointer(x, y);
    pendingMove.current = { x, y };
    click("left");
  };
  // A drag must never outlive the panel's control of the desktop.
  useEffect(() => {
    if (canControl || !buttons.current) return;
    buttons.current = 0;
    inject(pointerCommand(pointer.current.x, pointer.current.y, 0));
  }, [canControl, inject]);
  useEffect(
    () => () => {
      if (frame.current !== null) cancelAnimationFrame(frame.current);
    },
    [],
  );

  const sendKey = (key: DesktopKey, count = 1) => {
    const held = consumeModifiers();
    for (let index = 0; index < count; index += 1)
      inject(keyCommand(key, held));
  };

  // --- Keyboard ---
  const inputRef = useRef<TextInput>(null);
  // An uncontrolled hidden input: each edit is diffed against the previous
  // text. Resetting it while typing would race the keyboard, so it is only
  // replaced (remounted with the sentinel) once the keyboard has closed.
  const inputText = useRef(KEYBOARD_SENTINEL);
  const [inputGeneration, setInputGeneration] = useState(0);
  const [inputFocused, setInputFocused] = useState(false);
  const keyboard = useKeyboardState((state) => ({
    height: state.height,
    isVisible: state.isVisible,
  }));
  const keyboardOpen = active && inputFocused && keyboard.isVisible;
  const sendInput = (actions: KeyboardInputAction[]) => {
    if (!canControl || actions.length === 0) return;
    const held = consumeModifiers();
    for (const action of actions) {
      if (action.kind === "text") inject(textCommand(action.text, held));
      else
        for (let index = 0; index < action.count; index += 1)
          inject(keyCommand(action.key, held));
    }
  };
  const onChangeText = (text: string) => {
    const actions = diffKeyboardInput(text, inputText.current);
    inputText.current = text;
    sendInput(actions);
  };
  // Backspace in an emptied input changes no text; the key press still arrives.
  const onKeyPress = (key: string) => {
    if (key === "Backspace" && inputText.current === "")
      sendInput([{ kind: "key", key: DESKTOP_KEYS.backspace, count: 1 }]);
  };
  const onInputBlur = () => {
    setInputFocused(false);
    inputText.current = KEYBOARD_SENTINEL;
    setInputGeneration((value) => value + 1);
  };
  const showKeyboard = () => {
    const input = inputRef.current;
    if (!canControl || !input) return;
    // A keyboard hidden by the system (back gesture) leaves the input
    // focused, and focusing it again would not bring the keyboard back.
    if (input.isFocused()) {
      input.blur();
      setTimeout(() => inputRef.current?.focus(), 60);
    } else input.focus();
  };
  const dismissKeyboard = () => {
    inputRef.current?.blur();
    void KeyboardController.dismiss().catch(() => undefined);
  };
  useEffect(() => {
    if (!active || !canControl) inputRef.current?.blur();
  }, [active, canControl]);

  const onAccessoryKey = (id: string) => {
    const entry = DESKTOP_ACCESSORY_KEYS.find((item) => item.id === id);
    if (!entry) return;
    if (entry.kind === "modifier") {
      const next = new Set(modifiersRef.current);
      if (next.has(entry.modifier)) next.delete(entry.modifier);
      else next.add(entry.modifier);
      modifiersRef.current = next;
      setModifiers(next);
    } else sendKey(entry.key);
  };

  // --- Zoom ---
  const [zoomLevel, setZoomLevel] = useState(1);
  const fitToScreen = () => {
    const { zoom, pinch, panX, panY } = viewport;
    runOnUI(() => {
      "worklet";
      const current = zoom.value * pinch.value;
      if (current <= 1) return;
      panX.value = withTiming(0, { duration: 220 });
      panY.value = withTiming(0, { duration: 220 });
      pinch.value = withTiming(1 / zoom.value, { duration: 220 }, (done) => {
        if (!done) return;
        zoom.value = 1;
        pinch.value = 1;
      });
    })();
    setZoomLevel(1);
  };

  // --- Clipboard ---
  const pasteFromDevice = async () => {
    const text = await Clipboard.getStringAsync().catch(() => "");
    if (!text) {
      showToast({ title: "Clipboard is empty", tone: "info" });
      return;
    }
    try {
      await client.desktop.writeClipboard(workspaceId, text);
      inject(keyCommand(DESKTOP_KEYS.v, { ...NO_MODIFIERS, ctrl: true }));
    } catch {
      showToast({
        title: "Paste failed",
        message: "Unable to write to the desktop clipboard.",
        tone: "error",
      });
    }
  };
  const copyFromDesktop = async () => {
    try {
      const { text } = await client.desktop.readClipboard(workspaceId);
      if (!text) {
        showToast({ title: "Desktop clipboard is empty", tone: "info" });
        return;
      }
      await Clipboard.setStringAsync(text);
      // Android 13+ confirms copies itself.
      if (Platform.OS !== "android" || Number(Platform.Version) < 33)
        showToast({ title: "Copied from desktop", tone: "success" });
    } catch {
      showToast({
        title: "Copy failed",
        message: "Unable to read the desktop clipboard.",
        tone: "error",
      });
    }
  };
  const takeControl = () => {
    viewer.takeControl().catch(() =>
      showToast({
        title: "Could not take control",
        message: "Computer Use is still running. Try again.",
        tone: "error",
      }),
    );
  };

  const menuActions: NativeMenuAction[] = [
    {
      id: "paste",
      title: "Paste from clipboard",
      symbol: "doc.on.clipboard",
      disabled: !canControl,
    },
    {
      id: "copy",
      title: "Copy desktop clipboard",
      symbol: "doc.on.doc",
      disabled: !connected,
    },
    {
      id: "fit",
      title: "Fit to screen",
      symbol: "arrow.down.right.and.arrow.up.left",
      disabled: zoomLevel <= 1,
    },
    {
      id: "reconnect",
      title: "Reconnect",
      symbol: "arrow.clockwise",
      disabled: !snapshot.wanted,
    },
    snapshot.wanted
      ? {
          id: "stop",
          title: "Stop desktop",
          symbol: "stop.circle",
          destructive: true,
        }
      : { id: "start", title: "Start desktop", symbol: "play.circle" },
  ];
  const onMenu = (id: string) => {
    if (id === "paste") void pasteFromDevice();
    else if (id === "copy") void copyFromDesktop();
    else if (id === "fit") fitToScreen();
    else if (id === "reconnect") retry();
    else if (id === "stop") {
      dismissKeyboard();
      void coordinator.close(workspaceId);
    } else if (id === "start") void coordinator.acquire(workspaceId);
  };

  // --- Layout ---
  const [area, setArea] = useState<Size>({ width: 0, height: 0 });
  const onLayout = (event: LayoutChangeEvent) => {
    const { width, height } = event.nativeEvent.layout;
    setArea((current) =>
      current.width === width && current.height === height
        ? current
        : { width, height },
    );
  };
  const landscape = dimensions.width > dimensions.height;
  const bottom = keyboardOpen
    ? keyboard.height + KEY_ACCESSORY_HEIGHT
    : bottomInset;
  // The body's frame includes its padding: GAP on top, bottom + GAP below.
  const usable = {
    width: area.width - GAP * 2,
    height: Math.max(0, area.height - bottom - GAP * 2),
  };
  const trackpadMin = keyboardOpen
    ? TRACKPAD_MIN_KEYBOARD
    : TRACKPAD_MIN_HEIGHT;
  const box = landscape
    ? fitBox(fb, {
        width: usable.width * 0.62,
        height: usable.height,
      })
    : fitBox(fb, {
        width: usable.width,
        height: Math.max(0, usable.height - GAP - trackpadMin),
      });
  useEffect(() => {
    viewport.box.value = box;
  }, [box.width, box.height, viewport]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- Presentation ---
  const stopped = !snapshot.wanted && !snapshot.connecting;
  const startupFailed = snapshot.startupFailed;
  const status = stopped
    ? { label: "Stopped", color: theme.subtleText }
    : startupFailed || recoveryError
      ? { label: "Unavailable", color: theme.danger }
      : connected
        ? computerUseActive
          ? { label: "Computer Use is in control", color: theme.warning }
          : { label: "Connected", color: theme.success }
        : viewerStatus === "disconnected"
          ? { label: "Reconnecting…", color: theme.warning }
          : {
              label: proxyUrl ? "Connecting…" : "Starting…",
              color: theme.subtleText,
            };
  const busy = !stopped && !startupFailed && !recoveryError && !connected;

  const overlay = stopped ? (
    <ScreenMessage
      title="Desktop stopped"
      action={{
        title: "Start desktop",
        onPress: () => void coordinator.acquire(workspaceId),
      }}
      testID="desktop-stopped"
    />
  ) : startupFailed || recoveryError ? (
    <ScreenMessage
      title="Desktop connection failed"
      detail={startupFailed ? (snapshot.error ?? undefined) : undefined}
      action={{ title: "Try again", onPress: retry }}
      testID="desktop-failed"
    />
  ) : !connected ? (
    <ScreenMessage
      busy
      title={
        viewerStatus === "disconnected"
          ? "Desktop disconnected. Reconnecting…"
          : proxyUrl
            ? "Connecting to desktop…"
            : "Starting desktop…"
      }
      testID="desktop-connecting"
    />
  ) : computerUseActive ? (
    <View style={styles.computerUse} testID="desktop-computer-use">
      <MousePointerClick size={26} color="#FFFFFF" />
      <Text style={styles.computerUseText}>
        Computer Use is controlling this desktop. Taking control stops the
        agent.
      </Text>
      <Button
        title="Take control"
        variant="secondary"
        compact
        busy={viewer.takeControlBusy}
        onPress={takeControl}
        style={styles.messageAction}
        testID="desktop-take-control"
      />
    </View>
  ) : null;

  const trackpadStyle = landscape
    ? { flex: 1, alignSelf: "stretch" as const }
    : {
        flex: 1,
        alignSelf: "stretch" as const,
        minHeight: Math.min(trackpadMin, usable.height),
      };

  return (
    <View
      testID="desktop-panel"
      style={[styles.root, { backgroundColor: theme.background }]}
    >
      <View style={[styles.bar, { borderBottomColor: theme.border }]}>
        <View style={[styles.dot, { backgroundColor: status.color }]} />
        <Text
          style={[styles.status, { color: theme.mutedText }]}
          numberOfLines={1}
          testID="desktop-status"
        >
          {status.label}
        </Text>
        {busy ? (
          <ActivityIndicator size="small" color={theme.subtleText} />
        ) : null}
        <View style={styles.spacer} />
        {zoomLevel > 1 ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Zoom ${zoomLevel.toFixed(1)} times. Fit to screen`}
            onPress={fitToScreen}
            style={[styles.zoom, { backgroundColor: theme.controlFill }]}
            testID="desktop-zoom"
          >
            <Text style={[styles.zoomLabel, { color: theme.text }]}>
              {`${zoomLevel.toFixed(1)}×`}
            </Text>
          </Pressable>
        ) : null}
        <NativeMenu
          label="Desktop options"
          actions={menuActions}
          onSelect={onMenu}
          style={styles.menu}
          testID="desktop-menu"
        >
          <View style={styles.menuIcon}>
            <Ellipsis size={20} color={theme.text} />
          </View>
        </NativeMenu>
      </View>

      <View
        style={[
          styles.body,
          landscape ? styles.row : styles.column,
          { paddingBottom: bottom + GAP },
        ]}
        onLayout={onLayout}
      >
        {box.width > 0 ? (
          <DesktopScreen
            ref={screenRef}
            viewport={viewport}
            box={box}
            url={url}
            viewerKey={viewerKey}
            visible={connected}
            cursor={cursor}
            showCursor={canControl}
            interactive={canControl}
            onMessage={onMessage}
            onLoadEnd={() => {
              if (viewerStatusRef.current !== "connected")
                recovery.startTimeout();
            }}
            onLoadError={() => {
              if (viewerStatusRef.current === "connected")
                recovery.markDisconnected();
            }}
            onTapAt={tapAt}
            onZoomChange={setZoomLevel}
          >
            {overlay}
          </DesktopScreen>
        ) : null}
        <DesktopTrackpad
          viewport={viewport}
          enabled={canControl}
          onMove={onMove}
          onClick={click}
          onDrag={drag}
          onScroll={onScroll}
          style={trackpadStyle}
        />
      </View>

      <TextInput
        key={inputGeneration}
        ref={inputRef}
        defaultValue={KEYBOARD_SENTINEL}
        onChangeText={onChangeText}
        onKeyPress={(event) => onKeyPress(event.nativeEvent.key)}
        onSubmitEditing={() => {
          if (canControl) sendKey(DESKTOP_KEYS.enter);
        }}
        submitBehavior="submit"
        onFocus={() => setInputFocused(true)}
        onBlur={onInputBlur}
        autoCorrect={false}
        spellCheck={false}
        autoCapitalize="none"
        autoComplete="off"
        importantForAutofill="no"
        textContentType="none"
        smartInsertDelete={false}
        keyboardType={
          Platform.OS === "android" ? "visible-password" : "default"
        }
        keyboardAppearance={theme.scheme}
        returnKeyType="default"
        caretHidden
        contextMenuHidden
        accessibilityElementsHidden
        importantForAccessibility="no-hide-descendants"
        style={styles.hiddenInput}
        testID="desktop-keyboard-input"
      />

      {keyboardOpen && canControl ? (
        <KeyAccessoryBar
          keys={DESKTOP_ACCESSORY_KEYS.map((entry) => ({
            id: entry.id,
            label: entry.label,
            selected:
              entry.kind === "modifier" && modifiers.has(entry.modifier),
            uppercase: /^[a-z ]+$/.test(entry.label),
          }))}
          onPress={onAccessoryKey}
          onDismiss={dismissKeyboard}
          background={theme.background}
          foreground={theme.text}
          testIDPrefix="desktop"
        />
      ) : active && canControl && !keyboard.isVisible ? (
        <View
          style={[styles.keyboardButton, { bottom: bottomInset + GAP + 10 }]}
          pointerEvents="box-none"
        >
          <IconButton
            label="Show keyboard"
            symbol="keyboard"
            icon={<Keyboard size={20} color={theme.text} />}
            tone="floating"
            size={KEYBOARD_BUTTON_SIZE}
            onPress={showKeyboard}
            testID="desktop-show-keyboard"
          />
        </View>
      ) : null}
    </View>
  );
}

function ScreenMessage({
  title,
  detail,
  busy,
  action,
  testID,
}: {
  title: string;
  detail?: string;
  busy?: boolean;
  action?: { title: string; onPress: () => void };
  testID: string;
}): React.JSX.Element {
  return (
    <View style={styles.message} testID={testID}>
      {busy ? <ActivityIndicator color="#FFFFFF" /> : null}
      <Text style={styles.messageTitle}>{title}</Text>
      {detail ? (
        <Text style={styles.messageDetail} numberOfLines={3}>
          {detail}
        </Text>
      ) : null}
      {action ? (
        <Button
          title={action.title}
          variant="secondary"
          compact
          onPress={action.onPress}
          style={styles.messageAction}
          testID={`${testID}-action`}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  bar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingLeft: 16,
    paddingRight: 6,
    height: STATUS_BAR_HEIGHT,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  status: { fontSize: 13, fontWeight: "500", flexShrink: 1 },
  spacer: { flex: 1 },
  zoom: {
    height: 28,
    minWidth: 44,
    paddingHorizontal: 10,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  zoomLabel: { fontSize: 13, fontWeight: "600", fontVariant: ["tabular-nums"] },
  menu: { width: 44, height: 44 },
  menuIcon: {
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  body: { flex: 1, padding: GAP, gap: GAP },
  column: { flexDirection: "column", alignItems: "center" },
  row: { flexDirection: "row", alignItems: "flex-start" },
  message: {
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    paddingHorizontal: 20,
  },
  messageTitle: {
    color: "#FFFFFF",
    fontSize: 15,
    fontWeight: "600",
    textAlign: "center",
  },
  messageAction: { alignSelf: "center" },
  messageDetail: {
    color: "rgba(255, 255, 255, 0.72)",
    fontSize: 13,
    textAlign: "center",
  },
  computerUse: {
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    paddingHorizontal: 20,
    backgroundColor: "rgba(0, 0, 0, 0.55)",
  },
  computerUseText: {
    color: "#FFFFFF",
    fontSize: 13,
    textAlign: "center",
    maxWidth: 320,
  },
  hiddenInput: {
    position: "absolute",
    width: 1,
    height: 1,
    left: -100,
    top: 0,
    opacity: 0,
  },
  keyboardButton: { position: "absolute", right: GAP + 10 },
});
