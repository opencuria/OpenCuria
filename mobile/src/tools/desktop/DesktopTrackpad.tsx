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
import { gestureThresholdHaptic, lightTapHaptic } from "../../ui/haptics";
import { useTheme } from "../../ui/theme";
import type { DesktopViewport } from "./DesktopScreen";
import {
  trackpadCancel,
  trackpadDown,
  trackpadIdle,
  trackpadMove,
  trackpadUp,
  trackpadWake,
  type TrackpadStep,
} from "./desktopGestures";
import { followPointer, movePointer } from "./pointerMath";

export type MouseButton = "left" | "right";

export interface DesktopTrackpadProps {
  viewport: DesktopViewport;
  enabled: boolean;
  /** The pointer moved to this framebuffer pixel. */
  onMove: (x: number, y: number) => void;
  onClick: (button: MouseButton) => void;
  /** Press or release the left button (double tap or hold, then drag). */
  onButton: (down: boolean) => void;
  /** Scroll by pixel deltas (natural direction, like iOS). */
  onScroll: (deltaX: number, deltaY: number) => void;
  style?: StyleProp<ViewStyle>;
  children?: React.ReactNode;
}

const SCROLL_GAIN = 2.2;

/**
 * A laptop trackpad for the remote pointer. One finger moves with
 * acceleration; a quick tap clicks on release, a two-finger tap
 * right-clicks and two fingers scroll. Double tap and slide (or hold still)
 * to drag with the left button held. Recognition is the pure state machine
 * of `desktopGestures`, run on the UI thread.
 */
export function DesktopTrackpad({
  viewport,
  enabled,
  onMove,
  onClick,
  onButton,
  onScroll,
  style,
  children,
}: DesktopTrackpadProps): React.JSX.Element {
  const { theme } = useTheme();
  const { fb, box, zoom, panX, panY, pointerX, pointerY } = viewport;
  const machine = useSharedValue(trackpadIdle());
  const timer = useSharedValue(-1);
  /** 0 idle, 1 touched, 2 left button held. */
  const pressed = useSharedValue(0);
  const [hint, setHint] = useState(true);
  const used = () => setHint(false);

  const move = (deltaX: number, deltaY: number, speed: number) => {
    "worklet";
    const next = movePointer(
      { x: pointerX.value, y: pointerY.value },
      { x: deltaX, y: deltaY },
      speed,
      fb.value,
      box.value,
      zoom.value,
    );
    pointerX.value = next.x;
    pointerY.value = next.y;
    const pan = followPointer(next, fb.value, box.value, zoom.value, {
      x: panX.value,
      y: panY.value,
    });
    panX.value = pan.x;
    panY.value = pan.y;
    runOnJS(onMove)(next.x, next.y);
  };

  const apply = (step: TrackpadStep) => {
    "worklet";
    const before = machine.value.phase;
    machine.value = step.state;
    for (const action of step.actions) {
      if (action.type === "move") move(action.dx, action.dy, action.speed);
      else if (action.type === "scroll")
        runOnJS(onScroll)(-action.dx * SCROLL_GAIN, -action.dy * SCROLL_GAIN);
      else if (action.type === "click") runOnJS(onClick)(action.button);
      else if (action.type === "button") runOnJS(onButton)(action.down);
      else if (action.kind === "grab") runOnJS(gestureThresholdHaptic)();
      else runOnJS(lightTapHaptic)();
    }
    const phase = step.state.phase;
    if (phase === before) return;
    if (before === "idle") runOnJS(used)();
    const target =
      phase === "press" || phase === "drag" ? 2 : phase === "idle" ? 0 : 1;
    pressed.value = withTiming(target, { duration: target ? 100 : 180 });
  };

  const run = (step: TrackpadStep) => {
    "worklet";
    apply(step);
    if (step.wakeIn < 0) return;
    if (timer.value >= 0) clearTimeout(timer.value);
    const sequence = step.state.sequence;
    timer.value = setTimeout(() => {
      timer.value = -1;
      apply(trackpadWake(machine.value, sequence, performance.now()));
    }, step.wakeIn);
  };

  const gesture = Gesture.Manual()
    .enabled(enabled)
    .onTouchesDown((event, manager) => {
      manager.activate();
      run(trackpadDown(machine.value, event.changedTouches, performance.now()));
    })
    .onTouchesMove((event) => {
      run(trackpadMove(machine.value, event.allTouches, performance.now()));
    })
    .onTouchesUp((event, manager) => {
      run(trackpadUp(machine.value, event.changedTouches, performance.now()));
      if (machine.value.down.length === 0) manager.end();
    })
    .onTouchesCancelled((_event, manager) => {
      run(trackpadCancel(machine.value));
      manager.end();
    })
    .onFinalize(() => {
      // Ended from outside (disabled, view hidden): never keep a button down.
      if (machine.value.phase !== "idle") run(trackpadCancel(machine.value));
    });

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
              Double-tap and slide to drag
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
