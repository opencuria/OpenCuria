/**
 * Touch gesture recognition of the desktop panel as pure state machines: the
 * trackpad and the screen feed raw touches in and run the actions that come
 * out. All functions are worklets, so recognition, the native cursor and the
 * zoomed view run on the UI thread without waiting for JavaScript.
 *
 * Every trackpad touch is exactly one thing, decided by time and distance:
 * - tap: lifted quickly inside the slop circle → left click on release;
 * - move: leaving the slop circle → the pointer moves; it never clicks or
 *   grabs afterwards, and inside the circle the pointer stays put;
 * - hold: resting inside the circle → left button down, then drags;
 * - double tap: a touch right after a tap, next to it, presses the left
 *   button at once; lifted quickly it completes a double click, moved or
 *   held it drags.
 * Two fingers tapped together right-click, moved together they scroll.
 */

export interface Touch {
  id: number;
  x: number;
  y: number;
}

/** Thresholds in points and milliseconds. */
export const GESTURE = Object.freeze({
  /** A finger may drift this far and still tap or hold. */
  slop: 6,
  /** Longest touch that still counts as a tap. */
  tapMs: 200,
  /** Resting this long without moving grabs (left button down). */
  holdMs: 550,
  /** A touch this soon after a tap... */
  doubleTapMs: 280,
  /** ...and this close to it is the second touch of a double tap. */
  doubleTapSlop: 40,
  /** Holding a double tap's second touch this long confirms the grab. */
  grabMs: 300,
  /** Two fingers landing this close in time can right-click... */
  twoFingerJoinMs: 150,
  /** ...when both are lifted again within this time. */
  twoFingerTapMs: 320,
  /** A release this soon after the last movement keeps its momentum. */
  flingMs: 60,
  /** Slowest release (points/ms) that still flings the view. */
  flingSpeed: 0.15,
});

function distance(ax: number, ay: number, bx: number, by: number): number {
  "worklet";
  return Math.hypot(ax - bx, ay - by);
}

/**
 * Exponentially smoothed rate over ~40 ms, so single jittery samples do not
 * swing pointer acceleration or fling speed.
 */
function smooth(current: number, delta: number, dt: number): number {
  "worklet";
  if (dt <= 0) return current;
  const alpha = 1 - Math.exp(-dt / 40);
  return current + (delta / dt - current) * alpha;
}

/** The part of a displacement beyond the slop circle (no jump on start). */
function beyondSlop(dx: number, dy: number): { dx: number; dy: number } {
  "worklet";
  const length = Math.hypot(dx, dy);
  if (length <= GESTURE.slop) return { dx: 0, dy: 0 };
  const share = (length - GESTURE.slop) / length;
  return { dx: dx * share, dy: dy * share };
}

function find(touches: readonly Touch[], id: number): Touch | null {
  "worklet";
  for (let index = 0; index < touches.length; index += 1)
    if (touches[index].id === id) return touches[index];
  return null;
}

function without(ids: readonly number[], touches: readonly Touch[]): number[] {
  "worklet";
  return ids.filter((id) => find(touches, id) === null);
}

// --- Trackpad ---

export type TrackpadPhase =
  /** No finger on the pad. */
  | "idle"
  /** One finger, undecided: tap, hold or move. */
  | "touch"
  /** Second touch of a double tap: left button down, undecided. */
  | "press"
  /** The pointer follows the finger. */
  | "move"
  /** Left button down; the pointer follows the finger. */
  | "drag"
  /** Two fingers, undecided: right-click or scroll. */
  | "twoFingers"
  | "scroll"
  /** Done; waits until every finger has left the pad. */
  | "spent";

export interface TrackpadState {
  phase: TrackpadPhase;
  /** Every finger on the pad. */
  down: number[];
  /** The fingers the gesture follows (-1: none). */
  first: number;
  second: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** Where the reference point (finger or two-finger midpoint) started... */
  startX: number;
  startY: number;
  /** ...and where it was last applied. */
  lastX: number;
  lastY: number;
  /** Distance between two fingers when they landed. */
  span: number;
  startAt: number;
  lastAt: number;
  /** Smoothed finger speed in points/ms, for pointer acceleration. */
  speed: number;
  /** Two fingers landed together and stayed put: a right-click candidate. */
  together: boolean;
  /** The last tap, for double taps. */
  tapAt: number;
  tapX: number;
  tapY: number;
  /** Bumped by every first touch, so stale wake-ups are ignored. */
  sequence: number;
}

export type TrackpadAction =
  | { type: "move"; dx: number; dy: number; speed: number }
  | { type: "scroll"; dx: number; dy: number }
  | { type: "click"; button: "left" | "right" }
  | { type: "button"; down: boolean }
  | { type: "haptic"; kind: "click" | "grab" };

export interface TrackpadStep {
  state: TrackpadState;
  actions: TrackpadAction[];
  /** Call `trackpadWake` after this many milliseconds (-1: no call). */
  wakeIn: number;
}

export function trackpadIdle(): TrackpadState {
  "worklet";
  return {
    phase: "idle",
    down: [],
    first: -1,
    second: -1,
    x1: 0,
    y1: 0,
    x2: 0,
    y2: 0,
    startX: 0,
    startY: 0,
    lastX: 0,
    lastY: 0,
    span: 0,
    startAt: 0,
    lastAt: 0,
    speed: 0,
    together: false,
    tapAt: -Infinity,
    tapX: 0,
    tapY: 0,
    sequence: 0,
  };
}

/** Back to idle once no finger is left, else wait for the rest to lift. */
function settled(state: TrackpadState): TrackpadState {
  "worklet";
  if (state.down.length > 0) return { ...state, phase: "spent" };
  return { ...state, phase: "idle", first: -1, second: -1 };
}

export function trackpadDown(
  previous: TrackpadState,
  touches: readonly Touch[],
  now: number,
): TrackpadStep {
  "worklet";
  let state: TrackpadState = { ...previous, down: previous.down.slice() };
  const actions: TrackpadAction[] = [];
  let wakeIn = -1;
  for (const touch of touches) {
    if (state.down.indexOf(touch.id) !== -1) continue;
    state.down.push(touch.id);
    if (state.phase === "idle") {
      const doubleTap =
        now - state.tapAt <= GESTURE.doubleTapMs &&
        distance(touch.x, touch.y, state.tapX, state.tapY) <=
          GESTURE.doubleTapSlop;
      state = {
        ...state,
        phase: doubleTap ? "press" : "touch",
        first: touch.id,
        second: -1,
        x1: touch.x,
        y1: touch.y,
        startX: touch.x,
        startY: touch.y,
        lastX: touch.x,
        lastY: touch.y,
        startAt: now,
        lastAt: now,
        speed: 0,
        together: false,
        sequence: state.sequence + 1,
      };
      if (doubleTap) actions.push({ type: "button", down: true });
      wakeIn = doubleTap ? GESTURE.grabMs : GESTURE.holdMs;
    } else if (state.phase === "touch" || state.phase === "move") {
      // A second finger: right-click or scroll, never a hold any more.
      const midX = (state.x1 + touch.x) / 2;
      const midY = (state.y1 + touch.y) / 2;
      state = {
        ...state,
        phase: "twoFingers",
        second: touch.id,
        x2: touch.x,
        y2: touch.y,
        startX: midX,
        startY: midY,
        lastX: midX,
        lastY: midY,
        span: distance(state.x1, state.y1, touch.x, touch.y),
        together:
          state.phase === "touch" &&
          now - state.startAt <= GESTURE.twoFingerJoinMs,
      };
    } else if (state.phase === "twoFingers" || state.phase === "scroll") {
      state = { ...state, phase: "spent" };
    }
    // While the button is held, extra fingers are ignored.
  }
  return { state, actions, wakeIn };
}

export function trackpadMove(
  previous: TrackpadState,
  touches: readonly Touch[],
  now: number,
): TrackpadStep {
  "worklet";
  const actions: TrackpadAction[] = [];
  const first = find(touches, previous.first);
  const second = find(touches, previous.second);
  let state: TrackpadState = {
    ...previous,
    x1: first ? first.x : previous.x1,
    y1: first ? first.y : previous.y1,
    x2: second ? second.x : previous.x2,
    y2: second ? second.y : previous.y2,
  };
  const phase = state.phase;
  if (phase === "touch" || phase === "press") {
    const step = beyondSlop(state.x1 - state.startX, state.y1 - state.startY);
    if (step.dx === 0 && step.dy === 0) return { state, actions, wakeIn: -1 };
    state = {
      ...state,
      phase: phase === "touch" ? "move" : "drag",
      lastX: state.x1,
      lastY: state.y1,
      lastAt: now,
      tapAt: -Infinity,
    };
    if (phase === "press") actions.push({ type: "haptic", kind: "grab" });
    actions.push({ type: "move", dx: step.dx, dy: step.dy, speed: 0 });
  } else if (phase === "move" || phase === "drag") {
    const dx = state.x1 - state.lastX;
    const dy = state.y1 - state.lastY;
    if (dx === 0 && dy === 0) return { state, actions, wakeIn: -1 };
    const speed = smooth(state.speed, Math.hypot(dx, dy), now - state.lastAt);
    state = {
      ...state,
      lastX: state.x1,
      lastY: state.y1,
      lastAt: now,
      speed,
    };
    actions.push({ type: "move", dx, dy, speed });
  } else if (phase === "twoFingers") {
    const midX = (state.x1 + state.x2) / 2;
    const midY = (state.y1 + state.y2) / 2;
    const step = beyondSlop(midX - state.startX, midY - state.startY);
    const spread = Math.abs(
      distance(state.x1, state.y1, state.x2, state.y2) - state.span,
    );
    if (step.dx !== 0 || step.dy !== 0) {
      state = {
        ...state,
        phase: "scroll",
        lastX: midX,
        lastY: midY,
        together: false,
      };
      actions.push({ type: "scroll", dx: step.dx, dy: step.dy });
    } else if (spread > GESTURE.slop * 2) {
      // Fingers pinching apart or together: not a tap.
      state = { ...state, together: false };
    }
  } else if (phase === "scroll") {
    const midX = (state.x1 + state.x2) / 2;
    const midY = (state.y1 + state.y2) / 2;
    const dx = midX - state.lastX;
    const dy = midY - state.lastY;
    if (dx !== 0 || dy !== 0) {
      state = { ...state, lastX: midX, lastY: midY };
      actions.push({ type: "scroll", dx, dy });
    }
  }
  return { state, actions, wakeIn: -1 };
}

export function trackpadUp(
  previous: TrackpadState,
  touches: readonly Touch[],
  now: number,
): TrackpadStep {
  "worklet";
  const actions: TrackpadAction[] = [];
  let state: TrackpadState = {
    ...previous,
    down: without(previous.down, touches),
  };
  const firstUp = find(touches, state.first) !== null;
  const secondUp = find(touches, state.second) !== null;
  const quick = now - state.startAt <= GESTURE.tapMs;
  switch (state.phase) {
    case "touch":
      if (!firstUp) break;
      if (quick) {
        actions.push({ type: "click", button: "left" });
        actions.push({ type: "haptic", kind: "click" });
        state = {
          ...state,
          tapAt: now,
          tapX: state.startX,
          tapY: state.startY,
        };
      } else state = { ...state, tapAt: -Infinity };
      state = settled(state);
      break;
    case "press":
      if (!firstUp) break;
      // Lifted quickly, the press completes a double click.
      actions.push({ type: "button", down: false });
      if (quick) {
        actions.push({ type: "haptic", kind: "click" });
        state = { ...state, tapAt: now };
      } else state = { ...state, tapAt: -Infinity };
      state = settled(state);
      break;
    case "drag":
      if (!firstUp) break;
      actions.push({ type: "button", down: false });
      state = settled({ ...state, tapAt: -Infinity });
      break;
    case "move":
      if (!firstUp) break;
      state = settled({ ...state, tapAt: -Infinity });
      break;
    case "twoFingers":
      if (!firstUp && !secondUp) break;
      if (state.together && now - state.startAt <= GESTURE.twoFingerTapMs) {
        actions.push({ type: "click", button: "right" });
        actions.push({ type: "haptic", kind: "click" });
      }
      state = settled({ ...state, tapAt: -Infinity });
      break;
    case "scroll":
      if (!firstUp && !secondUp) break;
      state = settled({ ...state, tapAt: -Infinity });
      break;
    default:
      break;
  }
  if (state.down.length === 0 && state.phase !== "idle") {
    // Lost track of the gesture's finger: never leave the button down.
    if (state.phase === "press" || state.phase === "drag")
      actions.push({ type: "button", down: false });
    state = settled({ ...state, tapAt: -Infinity });
  }
  return { state, actions, wakeIn: -1 };
}

/** The touch was taken away (system gesture, view hidden): release. */
export function trackpadCancel(previous: TrackpadState): TrackpadStep {
  "worklet";
  const actions: TrackpadAction[] =
    previous.phase === "press" || previous.phase === "drag"
      ? [{ type: "button", down: false }]
      : [];
  return {
    state: { ...trackpadIdle(), sequence: previous.sequence },
    actions,
    wakeIn: -1,
  };
}

/** A wake-up requested by a step: a resting finger grabs. */
export function trackpadWake(
  previous: TrackpadState,
  sequence: number,
  now: number,
): TrackpadStep {
  "worklet";
  const unchanged = { state: previous, actions: [], wakeIn: -1 };
  if (sequence !== previous.sequence) return unchanged;
  const elapsed = now - previous.startAt;
  if (previous.phase === "touch" && elapsed >= GESTURE.holdMs)
    return {
      state: {
        ...previous,
        phase: "drag",
        lastX: previous.x1,
        lastY: previous.y1,
        lastAt: now,
        tapAt: -Infinity,
      },
      actions: [
        { type: "button", down: true },
        { type: "haptic", kind: "grab" },
      ],
      wakeIn: -1,
    };
  if (previous.phase === "press" && elapsed >= GESTURE.grabMs)
    return {
      state: {
        ...previous,
        phase: "drag",
        lastX: previous.x1,
        lastY: previous.y1,
        lastAt: now,
        tapAt: -Infinity,
      },
      actions: [{ type: "haptic", kind: "grab" }],
      wakeIn: -1,
    };
  return unchanged;
}

// --- Screen ---

export type ScreenPhase =
  /** No finger on the screen. */
  | "idle"
  /** One finger, undecided: tap or pan. */
  | "touch"
  /** One finger pans the zoomed view. */
  | "pan"
  /** Two fingers zoom around their midpoint and pan with it. */
  | "pinch";

export interface ScreenState {
  phase: ScreenPhase;
  down: number[];
  first: number;
  second: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  startX: number;
  startY: number;
  startAt: number;
  /** Reference point (finger or midpoint) when last applied. */
  lastX: number;
  lastY: number;
  /** Finger distance when last applied. */
  span: number;
  lastAt: number;
  /** Smoothed velocity of the reference point in points/ms. */
  vx: number;
  vy: number;
}

export type ScreenAction =
  /** Stop running view animations: a finger took over. */
  | { type: "stop" }
  /** A tap at a point of the screen box. */
  | { type: "tap"; x: number; y: number }
  /** The content follows the fingers by this delta. */
  | { type: "pan"; dx: number; dy: number }
  /** Scale the content by this factor around a point of the box. */
  | { type: "zoom"; scale: number; x: number; y: number }
  /** Released while moving: keep panning with this velocity (points/ms). */
  | { type: "fling"; vx: number; vy: number }
  /** The fingers are done changing the view. */
  | { type: "settle" };

export interface ScreenStep {
  state: ScreenState;
  actions: ScreenAction[];
}

export function screenIdle(): ScreenState {
  "worklet";
  return {
    phase: "idle",
    down: [],
    first: -1,
    second: -1,
    x1: 0,
    y1: 0,
    x2: 0,
    y2: 0,
    startX: 0,
    startY: 0,
    startAt: 0,
    lastX: 0,
    lastY: 0,
    span: 0,
    lastAt: 0,
    vx: 0,
    vy: 0,
  };
}

export function screenDown(
  previous: ScreenState,
  touches: readonly Touch[],
  now: number,
): ScreenStep {
  "worklet";
  let state: ScreenState = { ...previous, down: previous.down.slice() };
  const actions: ScreenAction[] = [];
  for (const touch of touches) {
    if (state.down.indexOf(touch.id) !== -1) continue;
    state.down.push(touch.id);
    if (state.phase === "idle") {
      actions.push({ type: "stop" });
      state = {
        ...state,
        phase: "touch",
        first: touch.id,
        second: -1,
        x1: touch.x,
        y1: touch.y,
        startX: touch.x,
        startY: touch.y,
        startAt: now,
        lastX: touch.x,
        lastY: touch.y,
        lastAt: now,
        vx: 0,
        vy: 0,
      };
    } else if (
      (state.phase === "touch" || state.phase === "pan") &&
      state.second === -1 &&
      state.first !== -1
    ) {
      state = {
        ...state,
        phase: "pinch",
        second: touch.id,
        x2: touch.x,
        y2: touch.y,
        lastX: (state.x1 + touch.x) / 2,
        lastY: (state.y1 + touch.y) / 2,
        span: distance(state.x1, state.y1, touch.x, touch.y),
      };
    }
    // More fingers do not change a pinch.
  }
  return { state, actions };
}

export function screenMove(
  previous: ScreenState,
  touches: readonly Touch[],
  now: number,
): ScreenStep {
  "worklet";
  const actions: ScreenAction[] = [];
  const first = find(touches, previous.first);
  const second = find(touches, previous.second);
  let state: ScreenState = {
    ...previous,
    x1: first ? first.x : previous.x1,
    y1: first ? first.y : previous.y1,
    x2: second ? second.x : previous.x2,
    y2: second ? second.y : previous.y2,
  };
  if (state.phase === "touch") {
    const step = beyondSlop(state.x1 - state.startX, state.y1 - state.startY);
    if (step.dx !== 0 || step.dy !== 0) {
      state = {
        ...state,
        phase: "pan",
        lastX: state.x1,
        lastY: state.y1,
        lastAt: now,
      };
      actions.push({ type: "pan", dx: step.dx, dy: step.dy });
    }
  } else if (state.phase === "pan") {
    const dx = state.x1 - state.lastX;
    const dy = state.y1 - state.lastY;
    if (dx !== 0 || dy !== 0) {
      const dt = now - state.lastAt;
      state = {
        ...state,
        lastX: state.x1,
        lastY: state.y1,
        lastAt: now,
        vx: smooth(state.vx, dx, dt),
        vy: smooth(state.vy, dy, dt),
      };
      actions.push({ type: "pan", dx, dy });
    }
  } else if (state.phase === "pinch") {
    const midX = (state.x1 + state.x2) / 2;
    const midY = (state.y1 + state.y2) / 2;
    const span = distance(state.x1, state.y1, state.x2, state.y2);
    const dx = midX - state.lastX;
    const dy = midY - state.lastY;
    if (dx !== 0 || dy !== 0) actions.push({ type: "pan", dx, dy });
    if (span > 0 && state.span > 0 && span !== state.span)
      actions.push({
        type: "zoom",
        scale: span / state.span,
        x: midX,
        y: midY,
      });
    state = { ...state, lastX: midX, lastY: midY, span, lastAt: now };
  }
  return { state, actions };
}

export function screenUp(
  previous: ScreenState,
  touches: readonly Touch[],
  now: number,
): ScreenStep {
  "worklet";
  const actions: ScreenAction[] = [];
  let state: ScreenState = {
    ...previous,
    down: without(previous.down, touches),
  };
  const firstUp = find(touches, state.first) !== null;
  const secondUp = find(touches, state.second) !== null;
  if (state.phase === "touch" && firstUp) {
    if (now - state.startAt <= GESTURE.tapMs)
      actions.push({ type: "tap", x: state.startX, y: state.startY });
    state = { ...state, phase: "idle", first: -1 };
  } else if (state.phase === "pan" && firstUp) {
    const speed = Math.hypot(state.vx, state.vy);
    if (now - state.lastAt <= GESTURE.flingMs && speed >= GESTURE.flingSpeed)
      actions.push({ type: "fling", vx: state.vx, vy: state.vy });
    else actions.push({ type: "settle" });
    state = { ...state, phase: "idle", first: -1 };
  } else if (state.phase === "pinch" && (firstUp || secondUp)) {
    actions.push({ type: "settle" });
    // The remaining finger keeps panning without a jump.
    const rest = firstUp ? state.second : state.first;
    const restX = firstUp ? state.x2 : state.x1;
    const restY = firstUp ? state.y2 : state.y1;
    state =
      firstUp && secondUp
        ? { ...state, phase: "idle", first: -1, second: -1 }
        : {
            ...state,
            phase: "pan",
            first: rest,
            second: -1,
            x1: restX,
            y1: restY,
            lastX: restX,
            lastY: restY,
            lastAt: now,
            vx: 0,
            vy: 0,
          };
  }
  if (state.down.length === 0 && state.phase !== "idle") {
    if (state.phase === "pan" || state.phase === "pinch")
      actions.push({ type: "settle" });
    state = { ...state, phase: "idle", first: -1, second: -1 };
  }
  return { state, actions };
}

export function screenCancel(previous: ScreenState): ScreenStep {
  "worklet";
  return {
    state: screenIdle(),
    actions:
      previous.phase === "pan" || previous.phase === "pinch"
        ? [{ type: "settle" }]
        : [],
  };
}
