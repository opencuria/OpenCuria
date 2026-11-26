import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  ActivityIndicator,
  Platform,
  StyleSheet,
  Text,
  View,
  useWindowDimensions,
} from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  FadeIn,
  FadeOut,
  useAnimatedStyle,
  useSharedValue,
  withSpring,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { runOnJS } from "react-native-worklets";
import { Check, X } from "lucide-react-native";
import { formatVoiceDuration } from "@opencuria/chat-core/voiceInput";
import { ComposerAction } from "../ui/ComposerAction";
import { ComposerSurface } from "../ui/ComposerSurface";
import { lightTapHaptic } from "../ui/haptics";
import { useInk } from "../ui/ink";
import { NativeSegmentedPicker } from "../ui/selection";
import { useTheme } from "../ui/theme";
import { ToastSurface } from "../ui/ToastSurface";
import type { PointerMode } from "@opencuria/chat-core/visualRecording";
import type { RecordingPhase } from "../voice/voiceRecording";
import {
  RecordingLight,
  VoiceWaveform,
  type ComposerVoiceMeter,
} from "./ComposerVoice";

/** The composer's toolbar row (44) in its surface padding (6). */
const BAR_HEIGHT = 56;
const EDGE = 12;
const MAX_WIDTH = 440;
const MODES_WIDTH = 128;
const MODES: Array<{
  value: PointerMode;
  label: string;
  accessibilityLabel: string;
}> = [
  {
    value: "use",
    label: "Use",
    accessibilityLabel: "Use: taps, scrolling and zoom work as usual",
  },
  {
    value: "point",
    label: "Point",
    accessibilityLabel: "Point: show something without tapping it",
  },
];

/**
 * Where the bar rests in its container (`[x, y, width, height]`): known
 * before it renders, so the companion's native layer passes every other
 * touch to the app right away. `bottomOffset` keeps it above bottom chrome.
 */
export function recordingBarRegion(
  container: { width: number; height: number },
  insets: { top: number; bottom: number },
  atTop: boolean,
  bottomOffset = 0,
): [number, number, number, number] {
  const width = Math.min(container.width - EDGE * 2, MAX_WIDTH);
  const top = atTop
    ? insets.top + EDGE
    : container.height - insets.bottom - bottomOffset - EDGE - BAR_HEIGHT;
  return [(container.width - width) / 2, top, width, BAR_HEIGHT];
}
const HINT_MS = 5_000;
const METER_RENDER_MS = 250;
const SPRING = {
  stiffness: 420,
  damping: 40,
  mass: 1,
  overshootClamping: true,
};

export interface RecordingBarProps {
  phase: RecordingPhase;
  /** Time and levels; only the bar re-renders with them. */
  meter: ComposerVoiceMeter;
  mode: PointerMode;
  /** Offer Use/Point: what is shown takes pointers. */
  modes: boolean;
  onModeChange: (mode: PointerMode) => void;
  onCancel: () => void;
  onFinish: () => void;
  /** At the top edge of its container, else at the bottom. */
  atTop: boolean;
  onAtTopChange: (atTop: boolean) => void;
  /** Shown for a few seconds as the bar appears, while it offers modes. */
  hint?: string;
  /** Rests this far above the bottom edge, e.g. above a thumbnail strip. */
  bottomOffset?: number;
}

/**
 * Floats over whatever covers the composer while a recording runs (an open
 * app, a picture, a file), in the chat composer's liquid glass: time and
 * level, Use (taps, scrolling and zoom work as usual) or Point (touches only
 * show something), discard and done. It can be dragged between the top and
 * the bottom edge when it covers something. Over a light app the glass turns
 * light whatever the theme; its content is drawn inside the glass in
 * adaptive ink (`useInk`). Its parent must fill the screen or viewer.
 */
export function RecordingBar({
  phase,
  meter,
  mode,
  modes,
  onModeChange,
  onCancel,
  onFinish,
  atTop,
  onAtTopChange,
  hint: hintText,
  bottomOffset = 0,
}: RecordingBarProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  // Four renders a second are enough for the time and level; the JavaScript
  // thread stays free for touches.
  const tick = useCallback(
    () => Math.floor(meter.getSnapshot().elapsedMs / METER_RENDER_MS),
    [meter],
  );
  useSyncExternalStore(meter.subscribe, tick, tick);
  const { elapsedMs, levels } = meter.getSnapshot();
  const { width: windowWidth, height: windowHeight } = useWindowDimensions();
  // The container: the screen over an app, the sheet of a file viewer.
  const [measured, setMeasured] = useState<{
    width: number;
    height: number;
  } | null>(null);
  const container = measured ?? { width: windowWidth, height: windowHeight };
  const [left, topY, width] = recordingBarRegion(container, insets, true);
  const bottomY = recordingBarRegion(container, insets, false, bottomOffset)[1];
  const restY = atTop ? topY : bottomY;
  const y = useSharedValue(restY);
  const start = useSharedValue(restY);
  const live = phase === "recording";
  const [hint, setHint] = useState(true);
  useEffect(() => {
    const timer = setTimeout(() => setHint(false), HINT_MS);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    y.value = withSpring(restY, SPRING);
  }, [restY, y]);

  const placed = useRef(onAtTopChange);
  placed.current = onAtTopChange;
  const settle = useCallback((top: boolean) => {
    lightTapHaptic();
    placed.current(top);
  }, []);
  const drag = useMemo(
    () =>
      Gesture.Pan()
        .activeOffsetY([-8, 8])
        .onStart(() => {
          "worklet";
          start.value = y.value;
        })
        .onUpdate((event) => {
          "worklet";
          y.value = Math.min(
            bottomY,
            Math.max(topY, start.value + event.translationY),
          );
        })
        .onEnd((event) => {
          "worklet";
          const top =
            event.velocityY < -500 ||
            (event.velocityY < 500 && y.value < (topY + bottomY) / 2);
          y.value = withSpring(top ? topY : bottomY, SPRING);
          runOnJS(settle)(top);
        }),
    [bottomY, settle, start, topY, y],
  );
  const position = useAnimatedStyle(() => ({
    transform: [{ translateY: y.value }],
  }));

  const choose = (next: string) => {
    if (next === mode || (next !== "use" && next !== "point")) return;
    lightTapHaptic();
    onModeChange(next);
  };

  return (
    <View
      // Above sheets on Android too: touches there go by elevation, not
      // order, and a sheet's elevation would take the bar's taps.
      collapsable={false}
      pointerEvents="box-none"
      style={[StyleSheet.absoluteFill, styles.layer]}
      onLayout={(event) => {
        const { width: nextWidth, height: nextHeight } =
          event.nativeEvent.layout;
        setMeasured((previous) =>
          previous?.width === nextWidth && previous.height === nextHeight
            ? previous
            : { width: nextWidth, height: nextHeight },
        );
      }}
    >
      <Animated.View
        pointerEvents="box-none"
        style={[styles.root, { left, width }, position]}
        testID="recording-bar"
      >
        {hint && live && modes && hintText ? (
          <Animated.View
            entering={FadeIn}
            exiting={FadeOut}
            pointerEvents="none"
            style={[
              styles.hint,
              atTop ? { top: BAR_HEIGHT + 8 } : { bottom: BAR_HEIGHT + 8 },
            ]}
          >
            <ToastSurface>
              <RecordingHint text={hintText} />
            </ToastSurface>
          </Animated.View>
        ) : null}
        <View
          style={[
            styles.float,
            Platform.OS === "android" && {
              backgroundColor: theme.surfaceMuted,
            },
          ]}
        >
          <ComposerSurface>
            <View style={styles.bar}>
              <GestureDetector gesture={drag}>
                <View
                  style={styles.meter}
                  accessible
                  accessibilityRole="text"
                  accessibilityLabel={
                    live
                      ? `Recording, ${formatVoiceDuration(elapsedMs)}`
                      : phase === "finishing"
                        ? "Saving recording"
                        : "Starting microphone"
                  }
                  accessibilityLiveRegion="polite"
                >
                  <RecordingMeter
                    phase={phase}
                    elapsedMs={elapsedMs}
                    levels={levels}
                  />
                </View>
              </GestureDetector>
              {modes ? (
                <View style={styles.modes}>
                  <NativeSegmentedPicker
                    label="Recording mode"
                    value={mode}
                    options={MODES}
                    onChange={choose}
                    testID="recording-mode"
                  />
                </View>
              ) : null}
              <ComposerAction
                label="Discard recording"
                icon={X}
                disabled={phase === "finishing"}
                onPress={onCancel}
                testID="recording-cancel"
              />
              <ComposerAction
                label="Done"
                icon={Check}
                primary
                disabled={!live}
                onPress={onFinish}
                testID="recording-done"
              />
            </View>
          </ComposerSurface>
        </View>
      </Animated.View>
    </View>
  );
}

/** Light, time and level; reads its ink inside the glass. */
function RecordingMeter({
  phase,
  elapsedMs,
  levels,
}: {
  phase: RecordingPhase;
  elapsedMs: number;
  levels: readonly number[];
}): React.JSX.Element {
  const ink = useInk();
  return (
    <>
      <RecordingLight active={phase === "recording"} />
      {phase === "finishing" ? (
        <ActivityIndicator size="small" color={ink.mutedText} />
      ) : (
        <Text
          testID="recording-elapsed"
          style={[styles.elapsed, { color: ink.text }]}
        >
          {formatVoiceDuration(elapsedMs)}
        </Text>
      )}
      <View style={styles.wave}>
        <VoiceWaveform levels={levels} color={ink.mutedText} />
      </View>
    </>
  );
}

function RecordingHint({ text }: { text: string }): React.JSX.Element {
  const ink = useInk();
  return <Text style={[styles.hintText, { color: ink.text }]}>{text}</Text>;
}

const styles = StyleSheet.create({
  // Transparent, so its elevation casts no shadow; only orders it on top.
  layer: { zIndex: 40, elevation: 40 },
  root: { position: "absolute", top: 0, height: BAR_HEIGHT },
  // Lifts the glass off the app below it.
  float: {
    borderRadius: BAR_HEIGHT / 2,
    borderCurve: "continuous",
    shadowColor: "#000",
    shadowOpacity: 0.16,
    shadowRadius: 14,
    shadowOffset: { width: 0, height: 4 },
    elevation: 8,
  },
  bar: {
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  meter: {
    flex: 1,
    minWidth: 0,
    height: 44,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingStart: 10,
  },
  elapsed: {
    fontSize: 15,
    fontWeight: "500",
    fontVariant: ["tabular-nums"],
  },
  wave: { flex: 1, minWidth: 0, overflow: "hidden" },
  modes: { width: MODES_WIDTH, justifyContent: "center" },
  hint: { position: "absolute", left: 8, right: 8 },
  hintText: {
    fontSize: 13,
    lineHeight: 18,
    textAlign: "center",
    paddingHorizontal: 14,
    paddingVertical: 9,
  },
});
