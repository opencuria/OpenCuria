// Adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-terminal/LICENSE.
import React, {
  memo,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  useSyncExternalStore,
  type Ref,
} from "react";
import {
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  type LayoutChangeEvent,
  type NativeSyntheticEvent,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import {
  resolveNativeTerminalSurfaceView,
  supportsNativeTerminalOutput,
  type NativeTerminalSurfaceHandle,
} from "./nativeTerminalModule";
import { buildGhosttyThemeConfig, type TerminalTheme } from "./terminalTheme";
import type { TerminalSession } from "./terminalSession";

export interface TerminalSurfaceHandle {
  /** The text fallback focuses its input; the native view follows `keyboardFocusRequest`. */
  readonly focusKeyboard: () => void;
}

export interface TerminalSurfaceProps {
  readonly ref?: Ref<TerminalSurfaceHandle>;
  readonly session: TerminalSession;
  readonly terminalKey: string;
  readonly fontSize: number;
  readonly theme: TerminalTheme;
  readonly scheme: "light" | "dark";
  readonly isRunning: boolean;
  readonly autoFocus?: boolean;
  readonly keyboardFocusRequest?: number;
  readonly captureRequest?: number;
  readonly onCapture?: (text: string) => void;
  readonly onInput: (data: string) => void;
  readonly onResize: (size: {
    readonly cols: number;
    readonly rows: number;
  }) => void;
  readonly style?: StyleProp<ViewStyle>;
}

const MONO = Platform.OS === "ios" ? "Menlo" : "monospace";
// eslint-disable-next-line no-control-regex
const ANSI =
  /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g;

function estimateGridSize(input: {
  readonly width: number;
  readonly height: number;
  readonly fontSize: number;
}): { readonly cols: number; readonly rows: number } {
  const cellWidth = input.fontSize * 0.62;
  const cellHeight = input.fontSize * 1.35;
  return {
    cols: Math.max(20, Math.min(400, Math.floor(input.width / cellWidth))),
    rows: Math.max(5, Math.min(200, Math.floor(input.height / cellHeight))),
  };
}

/** Plain text when the native terminal is missing from an older build. */
function useFallbackBuffer(session: TerminalSession): string {
  const generation = useSyncExternalStore(
    session.subscribe,
    () => session.getSnapshot().generation,
  );
  const [buffer, setBuffer] = useState(() => session.getHistory());
  useEffect(() => {
    setBuffer(session.getHistory());
    let frame: number | null = null;
    const stop = session.onOutput(() => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        setBuffer(session.getHistory());
      });
    });
    return () => {
      stop();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [generation, session]);
  return buffer.replace(ANSI, "").replace(/\r(?!\n)/g, "");
}

const FallbackTerminalSurface = memo(function FallbackTerminalSurface({
  ref,
  ...props
}: TerminalSurfaceProps) {
  const inputRef = useRef<TextInput>(null);
  const buffer = useFallbackBuffer(props.session);
  const { theme, fontSize } = props;

  useImperativeHandle(ref, () => ({
    focusKeyboard: () => {
      inputRef.current?.blur();
      requestAnimationFrame(() => inputRef.current?.focus());
    },
  }));

  return (
    <View
      style={[
        styles.fallback,
        { backgroundColor: theme.background },
        props.style,
      ]}
      onLayout={(event: LayoutChangeEvent) => {
        const { width, height } = event.nativeEvent.layout;
        props.onResize(estimateGridSize({ width, height, fontSize }));
      }}
    >
      <ScrollView
        style={styles.fill}
        contentContainerStyle={styles.fallbackContent}
      >
        <Text
          selectable
          style={{
            color: theme.foreground,
            fontFamily: MONO,
            fontSize,
            lineHeight: Math.round(fontSize * 1.35),
          }}
        >
          {buffer || "$ "}
        </Text>
      </ScrollView>
      <View style={[styles.fallbackInput, { borderTopColor: theme.border }]}>
        <TextInput
          ref={inputRef}
          autoCapitalize="none"
          autoCorrect={false}
          submitBehavior="submit"
          editable={props.isRunning}
          placeholder="Type and press return"
          placeholderTextColor={theme.mutedForeground}
          returnKeyType="send"
          style={[
            styles.fill,
            { color: theme.foreground, fontFamily: MONO, padding: 0 },
          ]}
          onSubmitEditing={(event) => {
            const text = event.nativeEvent.text;
            // Terminal Enter is CR. LF is Ctrl+J and raw-mode TUIs can treat it as J.
            if (text.length > 0) props.onInput(`${text}\r`);
            inputRef.current?.clear();
          }}
        />
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Send Ctrl-C"
          disabled={!props.isRunning}
          onPress={() => props.onInput("\u0003")}
          style={({ pressed }) => [
            styles.ctrlC,
            {
              backgroundColor: theme.border,
              opacity: !props.isRunning ? 0.35 : pressed ? 0.65 : 1,
            },
          ]}
        >
          <Text
            style={{ color: theme.foreground, fontSize: 12, fontWeight: "700" }}
          >
            Ctrl-C
          </Text>
        </Pressable>
      </View>
    </View>
  );
});

/**
 * libghostty terminal surface (Metal on iOS, libghostty-vt + Canvas on
 * Android). Output is streamed in order from the session; a remount replays
 * the retained history first.
 */
export const TerminalSurface = memo(function TerminalSurface(
  props: TerminalSurfaceProps,
) {
  const NativeView = resolveNativeTerminalSurfaceView();
  const streaming = supportsNativeTerminalOutput();
  const nativeRef = useRef<NativeTerminalSurfaceHandle>(null);
  const [nativeReady, setNativeReady] = useState(false);
  const { session, onInput, onResize } = props;
  const generation = useSyncExternalStore(
    session.subscribe,
    () => session.getSnapshot().generation,
  );

  useEffect(() => {
    const view = nativeRef.current;
    if (!NativeView || !streaming || !nativeReady || !view) return;
    // Writes are asynchronous native calls; a chain keeps their order.
    let queue: Promise<void> = Promise.resolve();
    let live = true;
    const write = (data: string, reset: boolean) => {
      queue = queue
        .then(() => (live ? view.writeOutput(data, reset) : undefined))
        .catch(() => undefined);
    };
    write(session.getHistory(), true);
    const stop = session.onOutput((data) => write(data, false));
    return () => {
      live = false;
      stop();
    };
  }, [NativeView, generation, nativeReady, session, streaming]);

  if (!NativeView || !streaming) return <FallbackTerminalSurface {...props} />;

  return (
    <View style={props.style}>
      <NativeView
        ref={nativeRef}
        appearanceScheme={props.scheme}
        autoFocus={props.autoFocus ?? true}
        readOnly={false}
        backgroundColor={props.theme.background}
        foregroundColor={props.theme.foreground}
        mutedForegroundColor={props.theme.mutedForeground}
        focusRequest={props.isRunning ? (props.keyboardFocusRequest ?? 0) : 0}
        terminalKey={props.terminalKey}
        initialBuffer=""
        fontSize={props.fontSize}
        sharedGrid={[]}
        style={styles.fill}
        themeConfig={buildGhosttyThemeConfig(props.theme)}
        captureRequest={props.captureRequest}
        onCapture={(event) => props.onCapture?.(event.nativeEvent.text)}
        onInput={(event: NativeSyntheticEvent<{ readonly data: string }>) => {
          if (props.isRunning) onInput(event.nativeEvent.data);
        }}
        onResize={(
          event: NativeSyntheticEvent<{
            readonly cols: number;
            readonly rows: number;
          }>,
        ) => {
          // The first grid event comes from the mounted native surface.
          setNativeReady(true);
          onResize({
            cols: event.nativeEvent.cols,
            rows: event.nativeEvent.rows,
          });
        }}
      />
    </View>
  );
});

const styles = StyleSheet.create({
  fill: { flex: 1 },
  fallback: { flex: 1, overflow: "hidden" },
  fallbackContent: { padding: 10, paddingBottom: 16 },
  fallbackInput: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    padding: 8,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  ctrlC: { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 8 },
});
