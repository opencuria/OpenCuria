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
import { RecordingLight, VoiceWaveform } from "../../components/ComposerVoice";
import { ComposerAction } from "../../ui/ComposerAction";
import { ComposerSurface } from "../../ui/ComposerSurface";
import { lightTapHaptic } from "../../ui/haptics";
import { NativeSegmentedPicker } from "../../ui/selection";
import { useTheme } from "../../ui/theme";
import { ToastSurface } from "../../ui/ToastSurface";
import type { CompanionRegion } from "./companionNative";
import type {
  AppRecordingMeterStore,
  AppRecordingMode,
  AppRecordingPhase,
} from "./useAppRecording";

/** The composer's toolbar row (44) in its surface padding (6). */
const BAR_HEIGHT = 56;
const EDGE = 12;
const MAX_WIDTH = 440;
const MODES_WIDTH = 128;
const MODES: Array<{
  value: AppRecordingMode;
  label: string;
  accessibilityLabel: string;
}> = [
  {
    value: "use",
    label: "Use",
    accessibilityLabel: "Use: the app gets your taps",
  },
  {
    value: "point",
    label: "Point",
    accessibilityLabel: "Point: show something without tapping it",
  },
];

/**
 * Where the bar rests on a screen: its region, known before it renders, so
 * the native layer passes every other touch to the app right away.
 */
export function recordingBarRegion(
  window: { width: number; height: number },
  insets: { top: number; bottom: number },
  atTop: boolean,
): CompanionRegion {
  const width = Math.min(window.width - EDGE * 2, MAX_WIDTH);
  const top = atTop
    ? insets.top + EDGE
    : window.height - insets.bottom - EDGE - BAR_HEIGHT;
  return [(window.width - width) / 2, top, width, BAR_HEIGHT];
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
  phase: AppRecordingPhase;
  /** Time and levels; only the bar re-renders with them. */
  meter: AppRecordingMeterStore;
  mode: AppRecordingMode;
  onModeChange: (mode: AppRecordingMode) => void;
  onCancel: () => void;
  onFinish: () => void;
  /** At the top edge of the screen, else at the bottom. */
  atTop: boolean;
  onAtTopChange: (atTop: boolean) => void;
}

/**
 * Floats over the app while a recording runs, in the chat composer's liquid
 * glass: time and level, Use (the app gets every touch) or Point (touches
 * only show something), discard and done. It rests at the top and can be
 * dragged to the bottom (and back) when it covers the app's controls.
 */
export function RecordingBar({
  phase,
  meter,
  mode,
  onModeChange,
  onCancel,
  onFinish,
  atTop,
  onAtTopChange,
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
  const window = { width: windowWidth, height: windowHeight };
  const [left, topY, width] = recordingBarRegion(window, insets, true);
  const bottomY = recordingBarRegion(window, insets, false)[1];
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
    <Animated.View
      pointerEvents="box-none"
      style={[styles.root, { left, width }, position]}
      testID="companion-recording-bar"
    >
      {hint && live ? (
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
            <Text style={[styles.hintText, { color: theme.text }]}>
              Use the app and talk. Switch to Point to show something without
              tapping it.
            </Text>
          </ToastSurface>
        </Animated.View>
      ) : null}
      <View
        style={[
          styles.float,
          Platform.OS === "android" && { backgroundColor: theme.surfaceMuted },
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
                <RecordingLight active={live} />
                {phase === "finishing" ? (
                  <ActivityIndicator size="small" color={theme.mutedText} />
                ) : (
                  <Text
                    testID="companion-recording-elapsed"
                    style={[styles.elapsed, { color: theme.text }]}
                  >
                    {formatVoiceDuration(elapsedMs)}
                  </Text>
                )}
                <View style={styles.wave}>
                  <VoiceWaveform levels={levels} color={theme.mutedText} />
                </View>
              </View>
            </GestureDetector>
            <View style={styles.modes}>
              <NativeSegmentedPicker
                label="Recording mode"
                value={mode}
                options={MODES}
                onChange={choose}
                testID="companion-mode"
              />
            </View>
            <ComposerAction
              label="Discard recording"
              icon={X}
              disabled={phase === "finishing"}
              onPress={onCancel}
              testID="companion-recording-cancel"
            />
            <ComposerAction
              label="Done"
              icon={Check}
              primary
              disabled={!live}
              onPress={onFinish}
              testID="companion-recording-done"
            />
          </View>
        </ComposerSurface>
      </View>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
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
