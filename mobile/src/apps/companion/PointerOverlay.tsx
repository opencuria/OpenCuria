import React, { useMemo } from "react";
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
import { statusColor } from "../../ui/statusColors";
import { useTheme } from "../../ui/theme";

const DOT = 18;
const HALO = 64;
/** The dot lingers briefly after the finger lifts, so it can be seen. */
const LINGER_MS = 700;

/**
 * Pointer mode over the app: touches never reach it; a laser dot follows the
 * finger instead, like the webapp's pointing cursor, and a red frame marks
 * the mode. The native layer records
 * where the finger was and what is under it.
 */
export function PointerOverlay(): React.JSX.Element {
  const { theme } = useTheme();
  const red = statusColor("critical", theme);
  const x = useSharedValue(-HALO);
  const y = useSharedValue(-HALO);
  const opacity = useSharedValue(0);
  const scale = useSharedValue(0.6);
  const gesture = useMemo(
    () =>
      Gesture.Pan()
        .minDistance(0)
        .maxPointers(1)
        .shouldCancelWhenOutside(false)
        .onBegin((event) => {
          "worklet";
          x.value = event.x;
          y.value = event.y;
          opacity.value = withTiming(1, { duration: 80 });
          scale.value = withTiming(1, { duration: 140 });
        })
        .onUpdate((event) => {
          "worklet";
          x.value = event.x;
          y.value = event.y;
        })
        .onFinalize(() => {
          "worklet";
          opacity.value = withDelay(
            LINGER_MS,
            withTiming(0, { duration: 260 }),
          );
          scale.value = withDelay(
            LINGER_MS,
            withTiming(0.6, { duration: 260 }),
          );
        }),
    [opacity, scale, x, y],
  );
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
      testID="companion-pointer-overlay"
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
