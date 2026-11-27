import React, { useEffect, useMemo, useRef } from "react";
import {
  Platform,
  Pressable,
  StyleSheet,
  View,
  useWindowDimensions,
} from "react-native";
import { Gesture, GestureDetector } from "react-native-gesture-handler";
import Animated, {
  Easing,
  interpolate,
  useAnimatedStyle,
  useDerivedValue,
  useSharedValue,
  withSpring,
  withTiming,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { runOnJS } from "react-native-worklets";
import { HiddenBottomContext } from "../../ui/chromeInset";
import { useTheme } from "../../ui/theme";

export type SheetDetent = "half" | "full";

/** Critically damped, so the sheet settles without bouncing past its detent. */
const SPRING = {
  stiffness: 520,
  damping: 52,
  mass: 1,
  overshootClamping: true,
};
/**
 * Carries a sheet dragged away on at the finger's speed. Slightly bouncy, so
 * it reaches the screen's edge in about a quarter second, where clamping ends
 * it.
 */
const LEAVE_SPRING = {
  stiffness: 320,
  damping: 30,
  mass: 1,
  overshootClamping: true,
};
const OPEN_MS = 320;
const CLOSE_MS = 240;
/** Share of the screen the sheet takes at half height. */
const HALF_SHARE = 0.6;
const MIN_HALF = 360;
/** Drag distance and speed that move the sheet to the next detent. */
const DETENT_DISTANCE = 64;
const DETENT_VELOCITY = 600;
const DISMISS_DISTANCE = 120;
const DISMISS_VELOCITY = 1100;
const CORNER_RADIUS = Platform.OS === "ios" ? 28 : 24;
/** Gap above the sheet at full height, below the status bar. */
const TOP_GAP = 8;
/** Strip above the navigation bar that holds the grabber. */
const GRABBER_STRIP = Platform.OS === "ios" ? 12 : 16;
/**
 * Sheet surface below the screen's edge. Pulled past full height, the sheet
 * gives a little (never into the status bar) and shows no gap beneath.
 */
const BOTTOM_BLEED = TOP_GAP * 2;
/** Vertical travel before a drag on the header moves the sheet. */
const DRAG_SLOP = 6;

export interface SheetGeometry {
  full: number;
  half: number;
}

/** Heights of the sheet at its two detents for a screen. */
export function sheetGeometry(
  windowHeight: number,
  topInset: number,
): SheetGeometry {
  const full = Math.max(0, windowHeight - topInset - TOP_GAP);
  const half = Math.round(
    Math.min(full, Math.max(MIN_HALF, windowHeight * HALF_SHARE)),
  );
  return { full, half };
}

/**
 * How far the sheet's content, always laid out at full height, reaches below
 * the screen at `offset` (0 at full height). Between half and full height it
 * is the offset itself, so the composer docked to the content's bottom stays
 * on the screen's edge. Below half height the composer leaves with the sheet.
 */
export function sheetHiddenBottom(
  offset: number,
  geometry: SheetGeometry,
): number {
  "worklet";
  return Math.min(offset, geometry.full - geometry.half);
}

/**
 * Where a drag that began at `start` and was released at `offset` with
 * `velocity` (points per second, down positive) takes the sheet. A short drag
 * or flick moves it one detent; from full height a flick only reaches half
 * height, unless the sheet was already pulled below it.
 */
export function sheetReleaseTarget(
  start: number,
  offset: number,
  velocity: number,
  geometry: SheetGeometry,
): SheetDetent | "dismiss" {
  "worklet";
  const restHalf = geometry.full - geometry.half;
  const fromFull = start < restHalf / 2;
  if (
    offset > restHalf + DISMISS_DISTANCE ||
    (velocity > DISMISS_VELOCITY && (!fromFull || offset > restHalf))
  )
    return "dismiss";
  if (fromFull)
    return velocity > DETENT_VELOCITY || offset > DETENT_DISTANCE
      ? "half"
      : "full";
  return velocity < -DETENT_VELOCITY || offset < restHalf - DETENT_DISTANCE
    ? "full"
    : "half";
}

export interface CompanionSheetProps {
  /** Shown, or slid away below the screen. */
  open: boolean;
  detent: SheetDetent;
  onDetentChange: (detent: SheetDetent) => void;
  /** The user dragged the sheet away or tapped beside it. */
  onDismiss: () => void;
  /** The sheet finished sliding away. */
  onHidden: () => void;
  /**
   * Height of the content's navigation bar below the grabber; the grabber
   * and the bar drag the sheet, everything below scrolls.
   */
  barHeight: number;
  children: React.ReactNode;
  testID?: string;
}

/**
 * A bottom sheet over an open app in two heights: half, with the app visible
 * above, and full. The grabber and the content's navigation bar drag it. The
 * content is laid out once at full height and only moves: at half height its
 * lower part rests below the screen (`HiddenBottomContext`), so the docked
 * composer stays put while the sheet grows or shrinks.
 */
export function CompanionSheet({
  open,
  detent,
  onDetentChange,
  onDismiss,
  onHidden,
  barHeight,
  children,
  testID,
}: CompanionSheetProps): React.JSX.Element {
  const { theme } = useTheme();
  const { height: windowHeight } = useWindowDimensions();
  const insets = useSafeAreaInsets();
  const geometry = useMemo(
    () => sheetGeometry(windowHeight, insets.top),
    [windowHeight, insets.top],
  );
  const restHalf = geometry.full - geometry.half;
  const hidden = geometry.full + 24;
  const rest = detent === "full" ? 0 : restHalf;
  const offset = useSharedValue(hidden);
  const start = useSharedValue(0);
  const latest = useRef({ onDetentChange, onDismiss, onHidden });
  latest.current = { onDetentChange, onDismiss, onHidden };
  /** The sheet rests below the screen: before its first opening, or hidden. */
  const away = useRef(true);
  /** A drag already carries the sheet away; closing must not restart it. */
  const draggedAway = useRef(false);
  const changeDetent = (next: SheetDetent) =>
    latest.current.onDetentChange(next);
  const dismissByDrag = () => {
    draggedAway.current = true;
    latest.current.onDismiss();
  };
  const hiddenNow = () => {
    away.current = true;
    latest.current.onHidden();
  };
  const hiddenBottom = useDerivedValue(
    () => sheetHiddenBottom(offset.value, geometry),
    [geometry],
  );

  // Opening slides in from below and closing slides away and reports it; a
  // new rest while open (a detent the screen chose, a rotated screen) springs.
  const shown = useRef({ open: false, rest });
  useEffect(() => {
    const previous = shown.current;
    shown.current = { open, rest };
    if (open && !previous.open) {
      draggedAway.current = false;
      if (away.current) offset.value = hidden;
      away.current = false;
      offset.value = withTiming(rest, {
        duration: OPEN_MS,
        easing: Easing.out(Easing.cubic),
      });
    } else if (open && rest !== previous.rest) {
      // After a drag this joins the release spring and keeps its speed.
      offset.value = withSpring(rest, SPRING);
    } else if (!open && previous.open && !draggedAway.current) {
      offset.value = withTiming(
        hidden,
        { duration: CLOSE_MS, easing: Easing.in(Easing.cubic) },
        (finished) => {
          if (finished) runOnJS(hiddenNow)();
        },
      );
    }
    // `hidden` is read when the sheet opens or closes; `hiddenNow` reads the
    // latest props.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rest, offset]);

  // Only the grabber and the navigation bar drag the sheet; their buttons
  // keep their taps, and touches below go to the content.
  const dragZone = GRABBER_STRIP + barHeight;
  const touchStart = useSharedValue({ x: 0, y: 0 });
  const pan = useMemo(
    () =>
      Gesture.Pan()
        .withTestId("companion-sheet-drag")
        // A closing sheet is on its way out; grabbing it would strand it.
        .enabled(open)
        .manualActivation(true)
        .onTouchesDown((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (!touch || event.numberOfTouches > 1 || touch.y > dragZone) {
            manager.fail();
            return;
          }
          touchStart.value = { x: touch.absoluteX, y: touch.absoluteY };
        })
        .onTouchesMove((event, manager) => {
          "worklet";
          const touch = event.allTouches[0];
          if (!touch) return;
          const dx = Math.abs(touch.absoluteX - touchStart.value.x);
          const dy = Math.abs(touch.absoluteY - touchStart.value.y);
          if (dx > DRAG_SLOP * 2 && dx > dy) manager.fail();
          else if (dy > DRAG_SLOP) manager.activate();
        })
        .onStart(() => {
          "worklet";
          start.value = offset.value;
        })
        .onUpdate((event) => {
          "worklet";
          const next = start.value + event.translationY;
          // A little give above full height, none past the screen's bottom.
          offset.value =
            next < 0 ? Math.max(-TOP_GAP, next / 4) : Math.min(next, hidden);
        })
        .onEnd((event) => {
          "worklet";
          // The sheet keeps the finger's speed on its way, like UIKit's
          // sheets, instead of stopping and starting over.
          const velocity = event.velocityY;
          const target = sheetReleaseTarget(
            start.value,
            offset.value,
            velocity,
            geometry,
          );
          if (target === "dismiss") {
            // Leaves right away on the UI thread; the screen only learns it.
            offset.value = withSpring(
              hidden,
              { ...LEAVE_SPRING, velocity },
              (finished) => {
                if (finished) runOnJS(hiddenNow)();
              },
            );
            runOnJS(dismissByDrag)();
            return;
          }
          offset.value = withSpring(target === "full" ? 0 : restHalf, {
            ...SPRING,
            velocity,
          });
          runOnJS(changeDetent)(target);
        }),
    // Callbacks read the latest props through a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open, dragZone, geometry, hidden, restHalf, offset, start, touchStart],
  );

  const sheetStyle = useAnimatedStyle(() => ({
    transform: [{ translateY: offset.value }],
  }));
  const scrimStyle = useAnimatedStyle(() => ({
    opacity: interpolate(
      offset.value,
      [0, restHalf, hidden],
      [0.42, 0.22, 0],
      "clamp",
    ),
  }));

  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="box-none">
      <Animated.View
        pointerEvents={open ? "auto" : "none"}
        style={[StyleSheet.absoluteFill, styles.scrim, scrimStyle]}
      >
        <Pressable
          style={StyleSheet.absoluteFill}
          accessibilityRole="button"
          accessibilityLabel="Close"
          onPress={onDismiss}
          testID={testID ? `${testID}-scrim` : undefined}
        />
      </Animated.View>
      <Animated.View
        testID={testID}
        style={[
          styles.sheet,
          {
            top: windowHeight - geometry.full,
            height: geometry.full + BOTTOM_BLEED,
            backgroundColor: theme.background,
          },
          sheetStyle,
        ]}
      >
        <HiddenBottomContext.Provider value={hiddenBottom}>
          <GestureDetector gesture={pan}>
            <View collapsable={false} style={{ height: geometry.full }}>
              <View style={styles.grabberStrip}>
                <View
                  style={[styles.grabber, { backgroundColor: theme.border }]}
                />
              </View>
              <View style={styles.content}>{children}</View>
            </View>
          </GestureDetector>
        </HiddenBottomContext.Provider>
      </Animated.View>
    </View>
  );
}

const styles = StyleSheet.create({
  scrim: { backgroundColor: "#000" },
  sheet: {
    position: "absolute",
    left: 0,
    right: 0,
    borderTopLeftRadius: CORNER_RADIUS,
    borderTopRightRadius: CORNER_RADIUS,
    borderCurve: "continuous",
    overflow: "hidden",
    shadowColor: "#000",
    shadowOpacity: 0.18,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: -2 },
    elevation: 24,
  },
  grabberStrip: {
    height: GRABBER_STRIP,
    alignItems: "center",
    paddingTop: Platform.OS === "ios" ? 5 : 8,
  },
  grabber: { width: 36, height: 5, borderRadius: 3 },
  content: { flex: 1 },
});
