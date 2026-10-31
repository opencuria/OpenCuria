import React, { useCallback, useMemo, useRef, useState } from "react";
import { Platform, StyleSheet, Text, View } from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  interpolate,
  useAnimatedStyle,
  useSharedValue,
  withTiming,
  type SharedValue,
} from "react-native-reanimated";
import { runOnJS } from "react-native-worklets";
import { ChevronUp } from "lucide-react-native";
import { useTheme } from "../ui/theme";

/**
 * Upward travel (dp) that claims the touch. Android's ScrollView takes a
 * vertical drag after its 8 dp touch slop, so the pull must claim it first;
 * at the end of the transcript that drag could not scroll anyway.
 */
const CLAIM = 6;
/** Upward travel that opens on release. */
const OPEN_DISTANCE = 72;
/** A quick upward flick opens with less travel. */
const OPEN_VELOCITY = 650;
const FLICK_DISTANCE = 24;
/** A finger resting this long is a hold (selection, context menu), not a pull. */
const HOLD_MS = 300;
/** A scroll event this recent means the list is still moving under the finger. */
const SETTLED_MS = 140;

export interface EndPullState {
  /** The transcript rests at its newest message. */
  atEnd: SharedValue<boolean>;
  /** `Date.now()` of the latest scroll event, so momentum never arms a pull. */
  lastScrollAt: SharedValue<number>;
}

/** Shared values the transcript keeps current for `useEndPull`. */
export function useEndPullState(): EndPullState {
  const atEnd = useSharedValue(true);
  const lastScrollAt = useSharedValue(0);
  return useMemo(() => ({ atEnd, lastScrollAt }), [atEnd, lastScrollAt]);
}

/**
 * Pull up past the newest message to open something above the chat. A pull
 * only arms when the finger lands while the transcript already rests at its
 * end: scrolling down to the end, momentum and reversing direction never
 * open it. Horizontal swipes (sidebar) and holds keep their own gestures.
 */
export function useEndPull(
  state: EndPullState,
  onOpen: (() => void) | undefined,
): {
  gesture: ReturnType<typeof Gesture.Pan>;
  progress: SharedValue<number>;
  ready: boolean;
} {
  const progress = useSharedValue(0);
  const armed = useSharedValue(false);
  const active = useSharedValue(false);
  const start = useSharedValue({ x: 0, y: 0, time: 0 });
  const [ready, setReady] = useState(false);
  const latest = useRef(onOpen);
  latest.current = onOpen;
  const open = useCallback(() => latest.current?.(), []);
  const enabled = Boolean(onOpen);
  const { atEnd, lastScrollAt } = state;

  const gesture = useMemo(
    () =>
      Gesture.Pan()
        .enabled(enabled)
        .manualActivation(true)
        .onTouchesDown((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          const now = Date.now();
          if (
            !touch ||
            event.numberOfTouches > 1 ||
            !atEnd.value ||
            now - lastScrollAt.value < SETTLED_MS
          ) {
            armed.value = false;
            manager.fail();
            return;
          }
          armed.value = true;
          start.value = { x: touch.absoluteX, y: touch.absoluteY, time: now };
        })
        .onTouchesMove((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (!touch || !armed.value) return;
          const dx = touch.absoluteX - start.value.x;
          const dy = touch.absoluteY - start.value.y;
          if (dy > CLAIM || Math.abs(dx) > Math.abs(dy) + 2) manager.fail();
          else if (Date.now() - start.value.time > HOLD_MS && dy > -CLAIM)
            manager.fail();
          else if (-dy >= CLAIM && -dy > Math.abs(dx) * 1.2) manager.activate();
        })
        .onTouchesUp((_event, manager) => {
          "worklet";
          if (!active.value) manager.fail();
        })
        .onStart(() => {
          "worklet";
          active.value = true;
        })
        .onUpdate((event) => {
          "worklet";
          const next = Math.max(0, -event.translationY) / OPEN_DISTANCE;
          if (progress.value >= 1 !== next >= 1) runOnJS(setReady)(next >= 1);
          progress.value = next;
        })
        .onEnd((event, success) => {
          "worklet";
          if (!success) return;
          const travel = -event.translationY;
          if (
            travel >= OPEN_DISTANCE ||
            (travel >= FLICK_DISTANCE && -event.velocityY >= OPEN_VELOCITY)
          )
            runOnJS(open)();
        })
        .onFinalize(() => {
          "worklet";
          armed.value = false;
          active.value = false;
          if (progress.value >= 1) runOnJS(setReady)(false);
          progress.value = withTiming(0, { duration: 180 });
        }),
    [active, armed, atEnd, enabled, lastScrollAt, open, progress, start],
  );
  return { gesture, progress, ready };
}

/** Wraps the transcript so a pull past its end can open the tools sheet. */
export function EndPullArea({
  gesture,
  children,
}: {
  gesture: ReturnType<typeof Gesture.Pan>;
  children: React.ReactElement;
}): React.JSX.Element {
  return <GestureDetector gesture={gesture}>{children}</GestureDetector>;
}

/** Quiet hint that follows the pull, above the composer. */
export function EndPullHint({
  progress,
  ready,
  label,
  bottom,
}: {
  progress: SharedValue<number>;
  ready: boolean;
  label: string;
  bottom: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  const style = useAnimatedStyle(() => {
    const value = Math.min(progress.value, 1.25);
    return {
      opacity: interpolate(value, [0, 0.25, 1], [0, 0.7, 1], "clamp"),
      transform: [
        { translateY: interpolate(value, [0, 1.25], [12, -10], "clamp") },
        { scale: interpolate(value, [0, 1, 1.1], [0.9, 1, 1.04], "clamp") },
      ],
    };
  });
  return (
    <Animated.View
      pointerEvents="none"
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      style={[styles.hint, { bottom }, style]}
    >
      <View
        style={[
          styles.capsule,
          {
            backgroundColor: theme.surfaceMuted,
            borderColor: theme.border,
          },
        ]}
      >
        <ChevronUp size={15} color={theme.mutedText} />
        <Text style={[styles.label, { color: theme.text }]}>
          {ready ? "Release to open" : label}
        </Text>
      </View>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  hint: { position: "absolute", left: 0, right: 0, alignItems: "center" },
  capsule: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 999,
    borderWidth: StyleSheet.hairlineWidth,
    ...(Platform.OS === "android" ? { elevation: 2 } : {}),
  },
  label: { fontSize: 13, fontWeight: "600" },
});
