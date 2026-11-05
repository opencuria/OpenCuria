import React, { useState } from "react";
import {
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withTiming,
} from "react-native-reanimated";
import { useTheme } from "../../ui/theme";
import type { DesktopViewport } from "./DesktopScreen";
import { followPointer, movePointer } from "./pointerMath";

export type MouseButton = "left" | "right";

export interface DesktopTrackpadProps {
  viewport: DesktopViewport;
  enabled: boolean;
  /** The pointer moved to this framebuffer pixel. */
  onMove: (x: number, y: number) => void;
  onClick: (button: MouseButton) => void;
  /** Press or release the left button for a drag. */
  onDrag: (down: boolean) => void;
  /** Scroll by pixel deltas (natural direction, like iOS). */
  onScroll: (deltaX: number, deltaY: number) => void;
  style?: StyleProp<ViewStyle>;
  children?: React.ReactNode;
}

/** Hold this long without moving to grab (left button down) for a drag. */
const DRAG_HOLD_MS = 320;
const SCROLL_GAIN = 2.2;

/**
 * A laptop trackpad for the remote pointer. One finger moves with
 * acceleration, a tap clicks, a two-finger tap right-clicks, two fingers
 * scroll and holding still before moving drags (left button held).
 */
export function DesktopTrackpad({
  viewport,
  enabled,
  onMove,
  onClick,
  onDrag,
  onScroll,
  style,
  children,
}: DesktopTrackpadProps): React.JSX.Element {
  const { theme } = useTheme();
  const { fb, box, zoom, pinch, panX, panY, pointerX, pointerY } = viewport;
  const pressed = useSharedValue(0);
  const [hint, setHint] = useState(true);
  const used = () => setHint(false);

  const move = (
    deltaX: number,
    deltaY: number,
    velocityX: number,
    velocityY: number,
  ) => {
    "worklet";
    const speed = Math.hypot(velocityX, velocityY) / 1000;
    const scale = zoom.value * pinch.value;
    const next = movePointer(
      { x: pointerX.value, y: pointerY.value },
      { x: deltaX, y: deltaY },
      speed,
      fb.value,
      box.value,
      scale,
    );
    pointerX.value = next.x;
    pointerY.value = next.y;
    const pan = followPointer(next, fb.value, box.value, scale, {
      x: panX.value,
      y: panY.value,
    });
    panX.value = pan.x;
    panY.value = pan.y;
    runOnJS(onMove)(next.x, next.y);
  };

  const pointerPan = Gesture.Pan()
    .enabled(enabled)
    .maxPointers(1)
    .minDistance(3)
    .onBegin(() => {
      pressed.value = withTiming(1, { duration: 120 });
    })
    .onStart(() => {
      runOnJS(used)();
    })
    .onChange((event) => {
      move(event.changeX, event.changeY, event.velocityX, event.velocityY);
    })
    .onFinalize(() => {
      pressed.value = withTiming(0, { duration: 180 });
    });

  const dragPan = Gesture.Pan()
    .enabled(enabled)
    .maxPointers(1)
    .activateAfterLongPress(DRAG_HOLD_MS)
    .onStart(() => {
      pressed.value = withTiming(2, { duration: 120 });
      runOnJS(onDrag)(true);
      runOnJS(used)();
    })
    .onChange((event) => {
      move(event.changeX, event.changeY, event.velocityX, event.velocityY);
    })
    .onFinalize((_event, success) => {
      pressed.value = withTiming(0, { duration: 180 });
      if (success) runOnJS(onDrag)(false);
    });

  const tap = Gesture.Tap()
    .enabled(enabled)
    .maxDuration(260)
    .maxDistance(12)
    .onBegin(() => {
      pressed.value = withTiming(1, { duration: 80 });
    })
    .onEnd((_event, success) => {
      if (success) runOnJS(onClick)("left");
    })
    .onFinalize(() => {
      pressed.value = withTiming(0, { duration: 180 });
    });

  const rightTap = Gesture.Tap()
    .enabled(enabled)
    .minPointers(2)
    .maxDuration(320)
    .maxDistance(20)
    .onEnd((_event, success) => {
      if (success) runOnJS(onClick)("right");
    });

  const scroll = Gesture.Pan()
    .enabled(enabled)
    .minPointers(2)
    .maxPointers(2)
    .minDistance(4)
    .onStart(() => {
      runOnJS(used)();
    })
    .onChange((event) => {
      runOnJS(onScroll)(
        -event.changeX * SCROLL_GAIN,
        -event.changeY * SCROLL_GAIN,
      );
    });

  const gesture = Gesture.Race(
    scroll,
    rightTap,
    dragPan,
    Gesture.Simultaneous(pointerPan, tap),
  );

  const idle = theme.border;
  const touched = theme.subtleText;
  const dragging = theme.primary;
  const surface = useAnimatedStyle(
    () => ({
      borderColor:
        pressed.value > 1.5 ? dragging : pressed.value > 0.05 ? touched : idle,
    }),
    [idle, touched, dragging],
  );

  return (
    <GestureDetector gesture={gesture}>
      <Animated.View
        testID="desktop-trackpad"
        accessibilityLabel="Trackpad"
        accessibilityHint="Move one finger to move the pointer, tap to click"
        style={[
          styles.pad,
          // Floating controls on the pad (keyboard) use surfaceMuted.
          { backgroundColor: theme.surface, opacity: enabled ? 1 : 0.45 },
          surface,
          style,
        ]}
      >
        {hint ? (
          <View style={styles.hint} pointerEvents="none">
            <Text style={[styles.hintText, { color: theme.subtleText }]}>
              Tap to click · Two fingers to scroll or right-click
            </Text>
            <Text style={[styles.hintText, { color: theme.subtleText }]}>
              Hold, then move to drag
            </Text>
          </View>
        ) : null}
        {children}
      </Animated.View>
    </GestureDetector>
  );
}

const styles = StyleSheet.create({
  pad: {
    borderRadius: 18,
    borderWidth: StyleSheet.hairlineWidth * 2,
    overflow: "hidden",
  },
  hint: {
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 24,
    gap: 4,
  },
  hintText: { fontSize: 13, textAlign: "center" },
});
