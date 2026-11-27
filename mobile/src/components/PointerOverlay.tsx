import React, { useMemo, useRef } from "react";
import { StyleSheet, View } from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  FadeIn,
  FadeOut,
  useAnimatedStyle,
  useSharedValue,
  withDelay,
  withTiming,
} from "react-native-reanimated";
import { runOnJS } from "react-native-worklets";
import { statusColor } from "../ui/statusColors";
import { useTheme } from "../ui/theme";

const DOT = 18;
const HALO = 64;
/** The dot lingers briefly after the finger lifts, so it can be seen. */
const LINGER_MS = 700;

/** A finger in pointer mode, in points of the overlay. */
export type PointerOverlayEvent = (
  event: "down" | "move" | "up",
  x: number,
  y: number,
) => void;

/**
 * Pointer mode over what is shown: touches never reach it; a laser dot
 * follows the finger instead, like the webapp's pointing cursor, and a red
 * frame marks the mode. Over an app the native layer records where the
 * finger was and what is under it; over pictures and files `onPointer`
 * reports it.
 */
export function PointerOverlay({
  onPointer,
  testID = "pointer-overlay",
}: {
  onPointer?: PointerOverlayEvent;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  const red = statusColor("critical", theme);
  const x = useSharedValue(-HALO);
  const y = useSharedValue(-HALO);
  const opacity = useSharedValue(0);
  const scale = useSharedValue(0.6);
  const reportRef = useRef(onPointer);
  reportRef.current = onPointer;
  const reports = Boolean(onPointer);
  const gesture = useMemo(() => {
    const report = (event: "down" | "move" | "up", px: number, py: number) =>
      reportRef.current?.(event, px, py);
    return Gesture.Pan()
      .withTestId(testID)
      .minDistance(0)
      .maxPointers(1)
      .shouldCancelWhenOutside(false)
      .onBegin((event) => {
        "worklet";
        x.value = event.x;
        y.value = event.y;
        opacity.value = withTiming(1, { duration: 80 });
        scale.value = withTiming(1, { duration: 140 });
        if (reports) runOnJS(report)("down", event.x, event.y);
      })
      .onUpdate((event) => {
        "worklet";
        x.value = event.x;
        y.value = event.y;
        if (reports) runOnJS(report)("move", event.x, event.y);
      })
      .onFinalize((event) => {
        "worklet";
        if (reports) runOnJS(report)("up", event.x, event.y);
        opacity.value = withDelay(LINGER_MS, withTiming(0, { duration: 260 }));
        scale.value = withDelay(LINGER_MS, withTiming(0.6, { duration: 260 }));
      });
  }, [opacity, reports, scale, testID, x, y]);
  const halo = useAnimatedStyle(() => ({
    opacity: opacity.value * 0.28,
    transform: [
      { translateX: x.value - HALO / 2 },
      { translateY: y.value - HALO / 2 },
      { scale: scale.value },
    ],
  }));
  const dot = useAnimatedStyle(() => ({
    opacity: opacity.value,
    transform: [
      { translateX: x.value - DOT / 2 },
      { translateY: y.value - DOT / 2 },
    ],
  }));
  return (
    <Animated.View
      entering={FadeIn.duration(160)}
      exiting={FadeOut.duration(160)}
      style={StyleSheet.absoluteFill}
      testID={testID}
    >
      <GestureDetector gesture={gesture}>
        <View style={StyleSheet.absoluteFill} collapsable={false}>
          <View
            pointerEvents="none"
            style={[styles.frame, { borderColor: red }]}
          />
          <Animated.View
            pointerEvents="none"
            style={[styles.halo, { backgroundColor: red }, halo]}
          />
          <Animated.View
            pointerEvents="none"
            style={[styles.dot, { backgroundColor: red }, dot]}
          />
        </View>
      </GestureDetector>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  frame: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    borderWidth: 3,
    opacity: 0.55,
  },
  halo: {
    position: "absolute",
    width: HALO,
    height: HALO,
    borderRadius: HALO / 2,
  },
  dot: {
    position: "absolute",
    width: DOT,
    height: DOT,
    borderRadius: DOT / 2,
    borderWidth: 2,
    borderColor: "#fff",
  },
});
