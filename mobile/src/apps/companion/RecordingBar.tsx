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
  Pressable,
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
import { Check, Hand, Pointer, X } from "lucide-react-native";
import { formatVoiceDuration } from "@opencuria/chat-core/voiceInput";
import { RecordingLight, VoiceWaveform } from "../../components/ComposerVoice";
import { ComposerAction } from "../../ui/ComposerAction";
import { lightTapHaptic } from "../../ui/haptics";
import { useTheme } from "../../ui/theme";
import type { CompanionRegion } from "./companionNative";
import type {
  AppRecordingMeterStore,
  AppRecordingMode,
  AppRecordingPhase,
} from "./useAppRecording";

const BAR_HEIGHT = 56;
const EDGE = 12;
const MAX_WIDTH = 440;

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
 * Floats over the app while a recording runs: time and level, Use (the app
 * gets every touch) or Point (touches only show something), discard and
 * done. It rests at the top and can be dragged to the bottom (and back)
 * when it covers the app's controls.
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

  const choose = (next: AppRecordingMode) => {
    if (next === mode) return;
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
            { backgroundColor: theme.text },
          ]}
        >
          <Text style={[styles.hintText, { color: theme.background }]}>
            Use the app and talk. Switch to Point to show something without
            tapping it.
          </Text>
        </Animated.View>
      ) : null}
      <View
        style={[
          styles.bar,
          { backgroundColor: theme.raised, borderColor: theme.separator },
        ]}
      >
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
        <View
          style={[styles.modes, { backgroundColor: theme.controlFill }]}
          accessibilityRole="radiogroup"
        >
          <ModeOption
            label="Use"
            hint="The app gets your taps"
            icon={Hand}
            selected={mode === "use"}
            onPress={() => choose("use")}
            testID="companion-mode-use"
          />
          <ModeOption
            label="Point"
            hint="Show something without tapping it"
            icon={Pointer}
            selected={mode === "point"}
            onPress={() => choose("point")}
            testID="companion-mode-point"
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
    </Animated.View>
  );
}

function ModeOption({
  label,
  hint,
  icon: Icon,
  selected,
  onPress,
  testID,
}: {
  label: string;
  hint: string;
  icon: typeof Hand;
  selected: boolean;
  onPress: () => void;
  testID: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Pressable
      accessibilityRole="radio"
      accessibilityLabel={label}
      accessibilityHint={hint}
      accessibilityState={{ selected }}
      // A segment switches on touch, not on release: the next finger on the
      // app must already go where the bar says.
      onPressIn={onPress}
      hitSlop={4}
      testID={testID}
      style={[
        styles.mode,
        selected && {
          backgroundColor: theme.background,
          shadowColor: "#000",
          shadowOpacity: 0.12,
          shadowRadius: 3,
          shadowOffset: { width: 0, height: 1 },
          elevation: 2,
        },
      ]}
    >
      <Icon
        size={16}
        strokeWidth={2.2}
        color={selected ? theme.text : theme.mutedText}
      />
      <Text
        style={[
          styles.modeLabel,
          { color: selected ? theme.text : theme.mutedText },
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  root: { position: "absolute", top: 0, height: BAR_HEIGHT },
  bar: {
    height: BAR_HEIGHT,
    borderRadius: BAR_HEIGHT / 2,
    borderCurve: "continuous",
    borderWidth: StyleSheet.hairlineWidth,
    flexDirection: "row",
    alignItems: "center",
    paddingStart: 14,
    paddingEnd: 6,
    gap: 4,
    shadowColor: "#000",
    shadowOpacity: 0.22,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 6 },
    elevation: 12,
  },
  meter: {
    flex: 1,
    minWidth: 0,
    height: BAR_HEIGHT,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  elapsed: {
    fontSize: 15,
    fontWeight: "600",
    fontVariant: ["tabular-nums"],
  },
  wave: { flex: 1, minWidth: 0, overflow: "hidden" },
  modes: {
    flexDirection: "row",
    borderRadius: 18,
    padding: 2,
    height: 36,
  },
  mode: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 10,
    borderRadius: 16,
  },
  modeLabel: { fontSize: 13, fontWeight: "600" },
  hint: {
    position: "absolute",
    left: 8,
    right: 8,
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingVertical: 9,
  },
  hintText: { fontSize: 13, lineHeight: 18, textAlign: "center" },
});
