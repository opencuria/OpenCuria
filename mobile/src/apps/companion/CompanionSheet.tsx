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
  const changeDetent = (next: SheetDetent) =>
    latest.current.onDetentChange(next);
  const dismiss = () => latest.current.onDismiss();
  const hiddenNow = () => latest.current.onHidden();
  const hiddenBottom = useDerivedValue(
    () => sheetHiddenBottom(offset.value, geometry),
    [geometry],
  );

  // Opening slides in from below; closing slides away and reports it.
  const positions = useRef({ rest, hidden });
  positions.current = { rest, hidden };
  const wasOpen = useRef(false);
  useEffect(() => {
    const { rest: target, hidden: away } = positions.current;
    if (open) {
      if (!wasOpen.current) offset.value = away;
      offset.value = withTiming(target, {
        duration: OPEN_MS,
        easing: Easing.out(Easing.cubic),
      });
    } else if (wasOpen.current) {
      offset.value = withTiming(
        away,
        { duration: CLOSE_MS, easing: Easing.in(Easing.cubic) },
        (finished) => {
          if (finished) runOnJS(hiddenNow)();
        },
      );
    }
    wasOpen.current = open;
    // Positions and `hiddenNow` are read when the sheet opens or closes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, offset]);

  // Only the grabber and the navigation bar drag the sheet; their buttons
  // keep their taps, and touches below go to the content.
  const dragZone = GRABBER_STRIP + barHeight;
  const touchStart = useSharedValue({ x: 0, y: 0 });
  const pan = useMemo(
    () =>
      Gesture.Pan()
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
          const position = offset.value;
          const velocity = event.velocityY;
          const fromFull = start.value < restHalf / 2;
          let target: "full" | "half" | "dismiss";
          if (
            velocity > DISMISS_VELOCITY ||
            position > restHalf + DISMISS_DISTANCE
          )
            target = "dismiss";
          else if (fromFull)
            target =
              velocity > DETENT_VELOCITY || position > DETENT_DISTANCE
                ? "half"
                : "full";
          else
            target =
              velocity < -DETENT_VELOCITY ||
              position < restHalf - DETENT_DISTANCE
                ? "full"
                : "half";
          if (target === "dismiss") {
            runOnJS(dismiss)();
            return;
          }
          offset.value = withSpring(target === "full" ? 0 : restHalf, SPRING);
          runOnJS(changeDetent)(target);
        }),
    // Callbacks read the latest props through a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dragZone, hidden, restHalf, offset, start, touchStart],
  );

  // A detent chosen by the screen (e.g. the keyboard opening) animates too.
  const shownDetent = useRef(detent);
  useEffect(() => {
    if (shownDetent.current === detent) return;
    shownDetent.current = detent;
    if (open) offset.value = withSpring(rest, SPRING);
  }, [detent, open, offset, rest]);

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
