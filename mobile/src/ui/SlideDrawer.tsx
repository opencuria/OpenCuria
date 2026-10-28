import React, { useCallback, useEffect, useMemo, useRef } from "react";
import {
  BackHandler,
  Platform,
  Pressable,
  StyleSheet,
  View,
  useWindowDimensions,
} from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import { KeyboardController } from "react-native-keyboard-controller";
import Animated, {
  useAnimatedStyle,
  useSharedValue,
  withSpring,
} from "react-native-reanimated";
import { runOnJS } from "react-native-worklets";
import { useTheme } from "./theme";

/** Critically damped, so the chat settles without bouncing past its edge. */
const SPRING = {
  stiffness: 1000,
  damping: 500,
  mass: 3,
  overshootClamping: true,
};
/** Horizontal travel before a swipe claims the touch. */
const SWIPE_SLOP = 10;
/** A finger resting this long is a hold (selection, context menu), not a swipe. */
const HOLD_MS = 250;
const FLING_VELOCITY = 500;
/** Part of the chat that stays visible beside the open sidebar. */
const CHAT_PEEK = 72;
const MAX_SIDEBAR_WIDTH = 360;
const CORNER_RADIUS = Platform.OS === "ios" ? 36 : 24;

export interface SlideDrawerProps {
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  /** Navigation revealed beneath the chat. */
  sidebar: React.ReactNode;
  /** The chat surface that slides aside. */
  children: React.ReactNode;
}

/**
 * Claude-style navigation: the chat slides right as a rounded, dimmed card and
 * reveals the sidebar beneath it. A horizontal swipe anywhere opens or closes
 * it, vertical scrolling and held fingers keep their own gestures, and tapping
 * the visible chat edge closes it again.
 */
export function SlideDrawer({
  open,
  onOpen,
  onClose,
  sidebar,
  children,
}: SlideDrawerProps): React.JSX.Element {
  const { theme } = useTheme();
  const { width } = useWindowDimensions();
  const sidebarWidth = Math.min(width - CHAT_PEEK, MAX_SIDEBAR_WIDTH);
  const progress = useSharedValue(open ? 1 : 0);
  const target = useSharedValue(open ? 1 : 0);
  const dragging = useSharedValue(false);
  const start = useSharedValue({ x: 0, y: 0, time: 0, progress: 0 });

  const latest = useRef({ onOpen, onClose });
  latest.current = { onOpen, onClose };
  const settle = useCallback((next: boolean) => {
    if (next) latest.current.onOpen();
    else latest.current.onClose();
  }, []);
  const dismissKeyboard = useCallback(() => {
    void KeyboardController.dismiss().catch(() => undefined);
  }, []);

  useEffect(() => {
    const next = open ? 1 : 0;
    if (open) dismissKeyboard();
    // A finished swipe already animates toward the state it reports.
    if (target.value === next) return;
    target.value = next;
    progress.value = withSpring(next, SPRING);
  }, [open, progress, target, dismissKeyboard]);

  useEffect(() => {
    if (!open || Platform.OS !== "android") return;
    const handler = BackHandler.addEventListener("hardwareBackPress", () => {
      latest.current.onClose();
      return true;
    });
    return () => handler.remove();
  }, [open]);

  const pan = useMemo(
    () =>
      Gesture.Pan()
        .manualActivation(true)
        .onTouchesDown((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (!touch || event.numberOfTouches > 1) {
            manager.fail();
            return;
          }
          start.value = {
            x: touch.absoluteX,
            y: touch.absoluteY,
            time: Date.now(),
            progress: progress.value,
          };
        })
        .onTouchesMove((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (!touch || dragging.value) return;
          const dx = touch.absoluteX - start.value.x;
          const dy = touch.absoluteY - start.value.y;
          const closed = target.value === 0;
          if (Math.abs(dy) > SWIPE_SLOP && Math.abs(dy) > Math.abs(dx))
            manager.fail();
          else if (Date.now() - start.value.time > HOLD_MS) manager.fail();
          else if (closed ? dx < -SWIPE_SLOP : dx > SWIPE_SLOP) manager.fail();
          else if (
            Math.abs(dx) > SWIPE_SLOP &&
            Math.abs(dx) > Math.abs(dy) * 1.5
          )
            manager.activate();
        })
        .onTouchesUp((_event, manager) => {
          "worklet";
          if (!dragging.value) manager.fail();
        })
        .onStart(() => {
          "worklet";
          dragging.value = true;
          runOnJS(dismissKeyboard)();
        })
        .onUpdate((event) => {
          "worklet";
          progress.value = Math.min(
            1,
            Math.max(
              0,
              start.value.progress + event.translationX / sidebarWidth,
            ),
          );
        })
        .onFinalize((event) => {
          "worklet";
          if (!dragging.value) return;
          dragging.value = false;
          // The final translation also covers moves coalesced after the last update.
          const position =
            start.value.progress + event.translationX / sidebarWidth;
          const next =
            Math.abs(event.velocityX) > FLING_VELOCITY
              ? event.velocityX > 0
                ? 1
                : 0
              : position > 0.5
                ? 1
                : 0;
          target.value = next;
          progress.value = withSpring(next, {
            ...SPRING,
            velocity: event.velocityX / sidebarWidth,
          });
          runOnJS(settle)(next === 1);
        }),
    [dismissKeyboard, dragging, progress, settle, sidebarWidth, start, target],
  );

  const sidebarStyle = useAnimatedStyle(() => ({
    opacity: 0.4 + progress.value * 0.6,
    transform: [{ translateX: (progress.value - 1) * sidebarWidth * 0.18 }],
  }));
  const chatStyle = useAnimatedStyle(() => ({
    transform: [{ translateX: progress.value * sidebarWidth }],
  }));
  const cornerStyle = useAnimatedStyle(() => ({
    borderRadius: progress.value * CORNER_RADIUS,
  }));
  const scrimStyle = useAnimatedStyle(() => ({
    opacity: progress.value,
    borderRadius: progress.value * CORNER_RADIUS,
  }));

  return (
    <GestureDetector gesture={pan}>
      <View style={[styles.root, { backgroundColor: theme.sidebar }]}>
        <Animated.View
          testID="conversation-drawer"
          pointerEvents={open ? "auto" : "none"}
          accessibilityElementsHidden={!open}
          importantForAccessibility={open ? "auto" : "no-hide-descendants"}
          style={[styles.sidebar, { width: sidebarWidth }, sidebarStyle]}
        >
          {sidebar}
        </Animated.View>
        <Animated.View style={[styles.fill, chatStyle]}>
          <Animated.View
            accessibilityElementsHidden={open}
            importantForAccessibility={open ? "no-hide-descendants" : "auto"}
            style={[
              styles.fill,
              styles.clip,
              { backgroundColor: theme.background },
              cornerStyle,
            ]}
          >
            {children}
          </Animated.View>
          <Animated.View
            pointerEvents="none"
            style={[
              styles.fill,
              {
                borderColor: theme.border,
                backgroundColor:
                  theme.scheme === "dark"
                    ? "rgba(0, 0, 0, 0.45)"
                    : "rgba(0, 0, 0, 0.08)",
              },
              styles.scrim,
              scrimStyle,
            ]}
          />
          {open ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Close sidebar"
              onPress={onClose}
              style={styles.fill}
              testID="drawer-scrim"
            />
          ) : null}
        </Animated.View>
      </View>
    </GestureDetector>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, overflow: "hidden" },
  fill: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0 },
  sidebar: { position: "absolute", top: 0, bottom: 0, left: 0 },
  clip: { overflow: "hidden" },
  scrim: { borderWidth: StyleSheet.hairlineWidth },
});
