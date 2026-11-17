import React, {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  AccessibilityInfo,
  Animated,
  Dimensions,
  Easing,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";

import {
  armAnchor,
  claimAnchor,
  disarmAnchor,
  type PromptFlightAnchor,
} from "./promptFlightAnchor";

/**
 * A sent prompt flies out of the composer into the transcript, as in the
 * webapp (FLIP): once its row is laid out and shown, it rises from where the
 * composer is at that moment (`promptFlightAnchor`).
 */
/** A row waiting for its transcript longer than this just shows. */
const SHOW_AFTER_MS = 1200;
const FLIGHT_MS = 320;
const FLIGHT_MAX_MS = 560;

let reduceMotion = false;
void AccessibilityInfo.isReduceMotionEnabled?.()
  .then((enabled) => {
    reduceMotion = enabled;
  })
  .catch(() => undefined);
AccessibilityInfo.addEventListener?.("reduceMotionChanged", (enabled) => {
  reduceMotion = enabled;
});

/**
 * Whether the transcript shows its rows. A list that is still placing its
 * first rows keeps them hidden; a flight waits for it.
 */
export const TranscriptReadyContext = createContext(true);

/** Arm a flight from the composer that `anchor` locates. */
export function armPromptFlight(anchor: PromptFlightAnchor): void {
  if (reduceMotion) disarmAnchor();
  else armAnchor(anchor);
}

/**
 * Row of a user message. A prompt just sent (`fly`) rises out of the
 * composer into its place. The decision is taken once, at mount, so the
 * server's copy replacing the prompt never restarts or cuts the flight.
 */
export function PromptFlight({
  fly,
  flightKey,
  style,
  testID,
  children,
}: React.PropsWithChildren<{
  fly: boolean;
  flightKey: string;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}>): React.JSX.Element {
  const ready = useContext(TranscriptReadyContext);
  const [from] = useState(() => (fly ? claimAnchor(flightKey) : null));
  const row = useRef<View>(null);
  const opacity = useRef(new Animated.Value(from === null ? 1 : 0)).current;
  const translateY = useRef(new Animated.Value(0)).current;
  const [laidOut, setLaidOut] = useState(false);
  /** The row shows: flying in, or at once. */
  const shown = useRef(false);
  const fallback = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (from === null) return;
    // Never leave a prompt hidden, whatever its transcript does.
    fallback.current = setTimeout(() => {
      shown.current = true;
      opacity.setValue(1);
    }, SHOW_AFTER_MS);
    return () => {
      if (fallback.current) clearTimeout(fallback.current);
    };
  }, [from, opacity]);

  useEffect(() => {
    if (from === null || !laidOut || !ready || shown.current) return;
    let frame = 0;
    let attempts = 0;
    const settle = () => {
      shown.current = true;
      if (fallback.current) clearTimeout(fallback.current);
    };
    const show = () => {
      settle();
      opacity.setValue(1);
    };
    const fly = (composer: number, y: number) => {
      const dy = composer - y;
      if (Math.abs(dy) < 1) return show();
      settle();
      translateY.setValue(dy);
      // The text leaves the composer: it shows from the first frame on.
      opacity.setValue(0.35);
      Animated.parallel([
        Animated.timing(opacity, {
          toValue: 1,
          duration: 90,
          useNativeDriver: true,
        }),
        Animated.timing(translateY, {
          toValue: 0,
          duration: Math.min(
            FLIGHT_MAX_MS,
            Math.max(FLIGHT_MS, 260 + Math.abs(dy) / 3),
          ),
          easing: Easing.bezier(0.22, 1, 0.36, 1),
          useNativeDriver: true,
        }),
      ]).start();
    };
    // Both positions are read at once; the row may still wait for its place.
    const measure = (): void => {
      const target = row.current;
      if (!target) return show();
      let composer: number | null | undefined;
      let own: number | undefined;
      const settled = () => {
        if (composer === undefined || own === undefined || shown.current)
          return;
        if (composer === null) return show();
        // Until the list places the row it sits far outside the window.
        const height = Dimensions.get("window").height;
        if (own > -height && own < height * 2) return fly(composer, own);
        attempts += 1;
        if (attempts > 10) return show();
        frame = requestAnimationFrame(measure);
      };
      from((y) => {
        composer = y;
        settled();
      });
      target.measureInWindow((_x, y) => {
        own = y;
        settled();
      });
    };
    // Measure once the transcript scrolled to its new end.
    frame = requestAnimationFrame(measure);
    return () => cancelAnimationFrame(frame);
  }, [from, laidOut, opacity, ready, translateY]);

  return (
    <Animated.View
      ref={row}
      testID={testID}
      onLayout={from !== null && !laidOut ? () => setLaidOut(true) : undefined}
      style={[style, from !== null && { opacity, transform: [{ translateY }] }]}
    >
      {children}
    </Animated.View>
  );
}
