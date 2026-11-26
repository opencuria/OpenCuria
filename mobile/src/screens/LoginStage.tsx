import React, { useEffect, useMemo, useRef } from "react";
import {
  StyleSheet,
  useWindowDimensions,
  View,
  type LayoutChangeEvent,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import Animated, {
  cancelAnimation,
  Easing,
  useAnimatedProps,
  useAnimatedStyle,
  useReducedMotion,
  useSharedValue,
  withDelay,
  withRepeat,
  withTiming,
  type SharedValue,
} from "react-native-reanimated";
import Svg, {
  Defs,
  G,
  LinearGradient,
  Path,
  RadialGradient,
  Rect,
  Stop,
} from "react-native-svg";
import {
  FACETS,
  LATTICE_RINGS as RINGS,
  ringOpacity,
  stagePaths,
  type CubePaths,
} from "./loginLattice";

/** Ground of the app icon: the stage is the icon opened to the full width. */
export const STAGE_GROUND = "#0B0A14";

/** One full turn of the facet chase while signing in. */
const CHASE_MS = 1500;
/** The mark's height in its own units (brand SVGs: 15 to 49). */
const MARK_HEIGHT = 34;
/** The mark never shrinks below this, however little room the form leaves. */
const MIN_SCALE = 0.3;

const AnimatedG = Animated.createAnimatedComponent(G);
const AnimatedPath = Animated.createAnimatedComponent(Path);

export interface LoginStageProps {
  /**
   * The parts of the sign-in that are complete. Each lights two facets of the
   * mark: the server its top, the email its left and the password its right
   * side, so the mark is whole once the form is.
   */
  server: boolean;
  email: boolean;
  password: boolean;
  /** Signing in: the facets chase around the mark and waves cross the lattice. */
  working: boolean;
  /**
   * Edges the stage runs under (status bar, an overlapping sheet); the mark
   * centers in the room between them.
   */
  insetTop?: number;
  insetBottom?: number;
  style?: StyleProp<ViewStyle>;
}

/**
 * The sign-in screen's hero: OpenCuria's cube amid a lattice of further cubes
 * (workspaces) on the app icon's ground. Purely decorative.
 */
export function LoginStage({
  server,
  email,
  password,
  working,
  insetTop = 0,
  insetBottom = 0,
  style,
}: LoginStageProps): React.JSX.Element {
  const window = useWindowDimensions();
  const reduceMotion = useReducedMotion();
  const width = window.width;
  const height = Math.round(Math.max(window.height, window.width) * 0.8);
  const unit = Math.min(4.6, Math.max(3.3, Math.min(width, 480) / 100));
  const cx = width / 2;
  const cy = height / 2;

  const paths = useMemo(
    () => stagePaths(width, height, unit),
    [width, height, unit],
  );

  const intro = useSharedValue(reduceMotion ? 1 : 0);
  useEffect(() => {
    if (reduceMotion) return;
    intro.value = withTiming(1, {
      duration: 1100,
      easing: Easing.out(Easing.cubic),
    });
  }, [intro, reduceMotion]);

  const top = useLit(server, reduceMotion);
  const left = useLit(email, reduceMotion);
  const right = useLit(password, reduceMotion);

  const work = useSharedValue(0);
  const clock = useSharedValue(0);
  useEffect(() => {
    if (working && !reduceMotion) {
      clock.value = 0;
      clock.value = withRepeat(
        withTiming(1, { duration: CHASE_MS, easing: Easing.linear }),
        -1,
      );
      work.value = withTiming(1, { duration: 220 });
      return;
    }
    work.value = withTiming(0, { duration: 320 }, (finished) => {
      if (finished) cancelAnimation(clock);
    });
  }, [working, reduceMotion, clock, work]);
  useEffect(() => () => cancelAnimation(clock), [clock]);

  // The lattice shrinks around the mark when the form needs the room (the
  // keyboard, an error, the server field) instead of cropping it.
  const room = useSharedValue(0);
  const markHeight = MARK_HEIGHT * unit;
  const artStyle = useAnimatedStyle(() => ({
    transform: [
      {
        scale: room.value
          ? Math.min(1, Math.max(MIN_SCALE, room.value / (markHeight * 1.7)))
          : 1,
      },
    ],
  }));
  const measureRoom = (event: LayoutChangeEvent) => {
    const next = event.nativeEvent.layout.height;
    room.value =
      room.value && !reduceMotion ? withTiming(next, { duration: 140 }) : next;
  };

  const lit = { top, left, right };
  return (
    <View
      style={[styles.stage, style]}
      accessible={false}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      pointerEvents="none"
    >
      {/* Larger than the stage and centered in it, so it never sizes it. */}
      <View
        onLayout={measureRoom}
        style={[styles.room, { top: insetTop, bottom: insetBottom }]}
      >
        <Animated.View style={[{ width, height }, artStyle]}>
          <Svg width={width} height={height}>
            <Defs>
              <LinearGradient id="oc-r" x1="0" y1="0" x2="0" y2="1">
                <Stop offset="0" stopColor="#9A8DFF" />
                <Stop offset="1" stopColor="#7464F2" />
              </LinearGradient>
              <LinearGradient id="oc-br" x1="0" y1="0" x2="1" y2="1">
                <Stop offset="0" stopColor="#6A5BE8" />
                <Stop offset="1" stopColor="#4C3FC4" />
              </LinearGradient>
              <RadialGradient
                id="oc-fade"
                gradientUnits="userSpaceOnUse"
                cx={cx}
                cy={cy}
                r={width * 0.72}
              >
                <Stop offset="0" stopColor={STAGE_GROUND} stopOpacity={0} />
                <Stop
                  offset="0.3"
                  stopColor={STAGE_GROUND}
                  stopOpacity={0.05}
                />
                <Stop offset="1" stopColor={STAGE_GROUND} stopOpacity={1} />
              </RadialGradient>
            </Defs>
            {paths.rings.map((ring, index) => (
              <Ring
                key={index}
                ring={index + 1}
                paths={ring}
                intro={intro}
                work={work}
                clock={clock}
              />
            ))}
            {/* The lattice dissolves into the ground away from the mark. */}
            <Rect width={width} height={height} fill="url(#oc-fade)" />
            <Ghost paths={paths.mark} intro={intro} />
            {FACETS.map((facet, index) => (
              <Facet
                key={index}
                d={paths.facets[index]}
                fill={facet.fill}
                order={index}
                lit={lit[facet.side]}
                work={work}
                clock={clock}
                hairline={0.3 * unit}
              />
            ))}
          </Svg>
        </Animated.View>
      </View>
    </View>
  );
}

/**
 * Eases toward 1 while `on`. The first lighting waits for the lattice's
 * entrance so a prefilled field assembles the mark rather than starting whole.
 */
function useLit(on: boolean, reduceMotion: boolean): SharedValue<number> {
  const value = useSharedValue(reduceMotion && on ? 1 : 0);
  const first = useRef(true);
  useEffect(() => {
    const target = on ? 1 : 0;
    const delay = first.current ? 520 : 0;
    first.current = false;
    if (reduceMotion) {
      value.value = target;
      return;
    }
    value.value = withDelay(
      delay,
      withTiming(target, {
        duration: on ? 420 : 240,
        easing: Easing.out(Easing.cubic),
      }),
    );
  }, [on, reduceMotion, value]);
  return value;
}

function Ring({
  ring,
  paths,
  intro,
  work,
  clock,
}: {
  ring: number;
  paths: CubePaths;
  intro: SharedValue<number>;
  work: SharedValue<number>;
  clock: SharedValue<number>;
}): React.JSX.Element {
  const rest = ringOpacity(ring);
  const animatedProps = useAnimatedProps(() => {
    // The entrance and every wave travel outward from the mark.
    const shown = Math.min(
      1,
      Math.max(0, intro.value * (RINGS + 1) - ring + 1),
    );
    const wave = clock.value * (RINGS + 2) - ring;
    const pulse = Math.max(0, 1 - Math.abs(wave - 0.5) * 1.6);
    return { opacity: shown * (rest + (1 - rest) * 0.85 * pulse * work.value) };
  });
  return (
    <AnimatedG animatedProps={animatedProps}>
      <Path d={paths.top} fill="#FFFFFF" fillOpacity={0.085} />
      <Path d={paths.left} fill="#9B95C9" fillOpacity={0.04} />
      <Path d={paths.right} fill="#7464F2" fillOpacity={0.16} />
      <Path
        d={paths.edges}
        fill="none"
        stroke="#D6D2F2"
        strokeOpacity={0.2}
        strokeWidth={1}
        strokeLinejoin="round"
      />
    </AnimatedG>
  );
}

/** The mark before its facets light: a cube outlined slightly above the rest. */
function Ghost({
  paths,
  intro,
}: {
  paths: CubePaths;
  intro: SharedValue<number>;
}): React.JSX.Element {
  const animatedProps = useAnimatedProps(() => ({
    opacity: Math.min(1, intro.value * (RINGS + 1)),
  }));
  return (
    <AnimatedG animatedProps={animatedProps}>
      <Path d={paths.top} fill="#FFFFFF" fillOpacity={0.12} />
      <Path d={paths.left} fill="#9B95C9" fillOpacity={0.07} />
      <Path d={paths.right} fill="#7464F2" fillOpacity={0.24} />
      <Path
        d={paths.edges}
        fill="none"
        stroke="#D6D2F2"
        strokeOpacity={0.5}
        strokeWidth={1.25}
        strokeLinejoin="round"
      />
    </AnimatedG>
  );
}

function Facet({
  d,
  fill,
  order,
  lit,
  work,
  clock,
  hairline,
}: {
  d: string;
  fill: string;
  order: number;
  lit: SharedValue<number>;
  work: SharedValue<number>;
  clock: SharedValue<number>;
  hairline: number;
}): React.JSX.Element {
  const animatedProps = useAnimatedProps(() => {
    // Each facet peaks in turn, as the webapp's working mark (14% up, 32% down).
    const phase = (clock.value - order / FACETS.length + 1) % 1;
    const peak =
      phase < 0.14
        ? phase / 0.14
        : phase < 0.32
          ? 1 - (phase - 0.14) / 0.18
          : 0;
    const chase = 1 - work.value * (1 - (0.28 + 0.72 * peak));
    return { opacity: lit.value * chase };
  });
  return (
    <AnimatedPath
      animatedProps={animatedProps}
      d={d}
      fill={fill}
      stroke="#14112A"
      strokeOpacity={0.55}
      strokeWidth={hairline}
      strokeLinejoin="round"
    />
  );
}

const styles = StyleSheet.create({
  stage: { backgroundColor: STAGE_GROUND, overflow: "hidden" },
  room: {
    position: "absolute",
    left: 0,
    right: 0,
    alignItems: "center",
    justifyContent: "center",
  },
});
