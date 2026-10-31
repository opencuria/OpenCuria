import React, { useCallback, useMemo, useRef, useState } from "react";
import {
  Platform,
  StyleSheet,
  Text,
  View,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";
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
import { gestureThresholdHaptic } from "../ui/haptics";
import { useTheme } from "../ui/theme";

/** Resting this close to the newest message still counts as its end. */
export const END_SLOP = 24;
/**
 * The transcript must rest this long after the reader's last scroll: momentum
 * is over, and a quick series of flicks down to the end never counts as a pull.
 */
const SETTLED_MS = 450;

/**
 * iOS: the scroll view always owns the drag; at the end it rubber-bands past
 * the newest message and the pull reads that overscroll. Points of visible
 * overscroll that open on release (about 80 points of finger travel).
 */
const BOUNCE_OPEN = 44;
/** A quick release opens with less overscroll. */
const BOUNCE_FLICK = 14;
/** UIKit release velocity, points per millisecond. */
const BOUNCE_VELOCITY = 0.5;

/**
 * Android: upward travel (dp) that claims the touch. The ScrollView takes a
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

const BOUNCES = Platform.OS === "ios";

type ScrollEvent = NativeSyntheticEvent<NativeScrollEvent>;

export interface EndPullState {
  /** The transcript rests at (or within `END_SLOP` of) its newest message. */
  atEnd: SharedValue<boolean>;
  /**
   * `Date.now()` of the latest scroll the reader drove (drag or momentum).
   * Programmatic scrolls, such as following a streaming reply, do not count.
   */
  lastScrollAt: SharedValue<number>;
}

/** Shared values the transcript keeps current for `useEndPull`. */
export function useEndPullState(): EndPullState {
  const atEnd = useSharedValue(true);
  const lastScrollAt = useSharedValue(0);
  return useMemo(() => ({ atEnd, lastScrollAt }), [atEnd, lastScrollAt]);
}

/** Distance between the visible end and the end of the scroll content. */
export function distanceFromEnd(event: ScrollEvent): number {
  const { contentOffset, contentSize, layoutMeasurement, contentInset } =
    event.nativeEvent;
  const end = Math.max(
    -(contentInset?.top ?? 0),
    contentSize.height + (contentInset?.bottom ?? 0) - layoutMeasurement.height,
  );
  return end - contentOffset.y;
}

/** Scroll callbacks the transcript forwards; they also track reader scrolls. */
export interface EndPullScrollHandlers {
  onScroll: (event: ScrollEvent) => void;
  onScrollBeginDrag: (event: ScrollEvent) => void;
  onScrollEndDrag: (event: ScrollEvent) => void;
  onMomentumScrollBegin: () => void;
  onMomentumScrollEnd: () => void;
}

/**
 * Pull up past the newest message to open something above the chat. A pull
 * only arms when the finger lands while the transcript already rests at its
 * end and no scroll of the reader is still moving: scrolling down to the end,
 * momentum and reversing direction never open it. Following a streaming
 * reply does not count as scrolling. Horizontal swipes (sidebar) and holds
 * keep their own gestures. Crossing the opening point ticks once (haptics).
 */
export function useEndPull(
  state: EndPullState,
  onOpen: (() => void) | undefined,
): {
  gesture: ReturnType<typeof Gesture.Pan>;
  progress: SharedValue<number>;
  ready: boolean;
  scroll: EndPullScrollHandlers;
} {
  const progress = useSharedValue(0);
  const armed = useSharedValue(false);
  const active = useSharedValue(false);
  const start = useSharedValue({ x: 0, y: 0, time: 0 });
  const [ready, setReadyState] = useState(false);
  const readyRef = useRef(false);
  const latest = useRef(onOpen);
  latest.current = onOpen;
  const enabled = Boolean(onOpen);
  const { atEnd, lastScrollAt } = state;

  const setReady = useCallback((next: boolean) => {
    if (readyRef.current === next) return;
    readyRef.current = next;
    setReadyState(next);
    if (next) gestureThresholdHaptic();
  }, []);
  /** Opens; a quick flick that never showed "Release to open" still ticks. */
  const open = useCallback(() => {
    if (!readyRef.current) gestureThresholdHaptic();
    latest.current?.();
  }, []);

  // -- Android: claim the drag before the ScrollView does ----------------------
  const gesture = useMemo(
    () =>
      Gesture.Pan()
        .enabled(enabled && !BOUNCES)
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
    [
      active,
      armed,
      atEnd,
      enabled,
      lastScrollAt,
      open,
      progress,
      setReady,
      start,
    ],
  );

  // -- Scroll tracking; on iOS also the rubber-band pull ------------------------
  const drag = useRef({
    dragging: false,
    momentum: false,
    armed: false,
    startOffset: 0,
  });
  const scroll = useMemo<EndPullScrollHandlers>(() => {
    const reset = () => {
      drag.current.armed = false;
      setReady(false);
      progress.value = withTiming(0, { duration: 180 });
    };
    /** Overscroll past the drag's start at the end; negative scrolls back. */
    const travel = (event: ScrollEvent) =>
      event.nativeEvent.contentOffset.y - drag.current.startOffset;
    return {
      onScroll: (event) => {
        const current = drag.current;
        if (current.dragging || current.momentum) lastScrollAt.value = Date.now();
        if (!BOUNCES || !current.armed || !current.dragging) return;
        const distance = travel(event);
        if (distance < -END_SLOP) {
          // Scrolled back into the transcript: this drag is reading, not pulling.
          reset();
          return;
        }
        const next = Math.max(0, distance) / BOUNCE_OPEN;
        progress.value = next;
        setReady(next >= 1);
      },
      onScrollBeginDrag: (event) => {
        const current = drag.current;
        const settled = Date.now() - lastScrollAt.value >= SETTLED_MS;
        current.dragging = true;
        current.momentum = false;
        current.armed =
          BOUNCES &&
          enabled &&
          settled &&
          (atEnd.value || distanceFromEnd(event) <= END_SLOP);
        // The drag begins after UIKit's slop and may already overscroll a
        // little; count that, but never trust more than the slop.
        current.startOffset =
          event.nativeEvent.contentOffset.y -
          Math.min(END_SLOP, Math.max(0, -distanceFromEnd(event)));
        lastScrollAt.value = Date.now();
      },
      onScrollEndDrag: (event) => {
        const current = drag.current;
        current.dragging = false;
        lastScrollAt.value = Date.now();
        if (!current.armed) return;
        const distance = travel(event);
        const velocity = event.nativeEvent.velocity?.y ?? 0;
        const opens =
          distance >= BOUNCE_OPEN ||
          (distance >= BOUNCE_FLICK && velocity >= BOUNCE_VELOCITY);
        if (opens) open();
        reset();
      },
      onMomentumScrollBegin: () => {
        drag.current.momentum = true;
        lastScrollAt.value = Date.now();
      },
      onMomentumScrollEnd: () => {
        drag.current.momentum = false;
        lastScrollAt.value = Date.now();
      },
    };
  }, [atEnd, enabled, lastScrollAt, open, progress, setReady]);

  return { gesture, progress, ready, scroll };
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
