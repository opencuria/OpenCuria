import { describe, expect, it } from "vitest";
import {
  GESTURE,
  screenDown,
  screenIdle,
  screenMove,
  screenUp,
  trackpadCancel,
  trackpadDown,
  trackpadIdle,
  trackpadMove,
  trackpadUp,
  trackpadWake,
  type ScreenAction,
  type ScreenState,
  type TrackpadAction,
  type TrackpadState,
} from "./desktopGestures";

/** Drives the trackpad machine like the gesture handler does. */
function trackpad() {
  let state: TrackpadState = trackpadIdle();
  let actions: TrackpadAction[] = [];
  let wake: { at: number; sequence: number } | null = null;
  const record = (
    step: { state: TrackpadState; actions: TrackpadAction[]; wakeIn: number },
    now: number,
  ) => {
    state = step.state;
    actions.push(...step.actions);
    if (step.wakeIn >= 0)
      wake = { at: now + step.wakeIn, sequence: step.state.sequence };
  };
  /** Advance the clock, firing a due wake-up like the UI-thread timer. */
  const until = (now: number) => {
    if (wake && wake.at <= now) {
      const due = wake;
      wake = null;
      record(trackpadWake(state, due.sequence, due.at), due.at);
    }
  };
  return {
    down(now: number, id: number, x: number, y: number) {
      until(now);
      record(trackpadDown(state, [{ id, x, y }], now), now);
    },
    move(now: number, ...touches: [number, number, number][]) {
      until(now);
      record(
        trackpadMove(
          state,
          touches.map(([id, x, y]) => ({ id, x, y })),
          now,
        ),
        now,
      );
    },
    up(now: number, id: number, x = 0, y = 0) {
      until(now);
      record(trackpadUp(state, [{ id, x, y }], now), now);
    },
    until,
    cancel() {
      record(trackpadCancel(state), 0);
    },
    /** Actions since the last call, without pointer movement details. */
    take(): string[] {
      const taken = actions.map((action) =>
        action.type === "move"
          ? "move"
          : action.type === "scroll"
            ? "scroll"
            : action.type === "click"
              ? `click:${action.button}`
              : action.type === "button"
                ? action.down
                  ? "button:down"
                  : "button:up"
                : `haptic:${action.kind}`,
      );
      actions = [];
      return taken;
    },
    moved(): { dx: number; dy: number } {
      const total = { dx: 0, dy: 0 };
      for (const action of actions)
        if (action.type === "move") {
          total.dx += action.dx;
          total.dy += action.dy;
        }
      return total;
    },
    get phase() {
      return state.phase;
    },
  };
}

describe("trackpad: tap or move", () => {
  it("clicks on release of a quick tap that stayed put", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.move(40, [1, 102, 101]);
    expect(pad.take()).toEqual([]);
    pad.up(90, 1);
    expect(pad.take()).toEqual(["click:left", "haptic:click"]);
    expect(pad.phase).toBe("idle");
  });

  it("keeps the pointer still inside the slop and never clicks after moving", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.move(30, [1, 104, 100]);
    expect(pad.take()).toEqual([]);
    // A short, quick nudge out of the slop circle moves instead of clicking.
    pad.move(60, [1, 110, 100]);
    expect(pad.moved().dx).toBeCloseTo(110 - 100 - GESTURE.slop);
    expect(pad.take()).toEqual(["move"]);
    pad.move(80, [1, 104, 100]);
    pad.up(100, 1);
    expect(pad.take()).toEqual(["move"]);
  });

  it("does not click when the finger rested too long", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.up(GESTURE.tapMs + 50, 1);
    expect(pad.take()).toEqual([]);
  });

  it("accelerates with the finger speed once moving", () => {
    const fast = trackpadMove(
      { ...trackpadIdle(), phase: "move", first: 1, lastX: 0, lastAt: 0 },
      [{ id: 1, x: 30, y: 0 }],
      16,
    );
    const slow = trackpadMove(
      { ...trackpadIdle(), phase: "move", first: 1, lastX: 0, lastAt: 0 },
      [{ id: 1, x: 2, y: 0 }],
      16,
    );
    const speedOf = (actions: TrackpadAction[]) =>
      actions[0]?.type === "move" ? actions[0].speed : 0;
    expect(speedOf(fast.actions)).toBeGreaterThan(speedOf(slow.actions));
  });
});

describe("trackpad: hold", () => {
  it("grabs after resting still and drags until release", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.move(200, [1, 102, 102]);
    pad.until(GESTURE.holdMs);
    expect(pad.take()).toEqual(["button:down", "haptic:grab"]);
    expect(pad.phase).toBe("drag");
    pad.move(GESTURE.holdMs + 20, [1, 140, 102]);
    expect(pad.take()).toEqual(["move"]);
    pad.up(GESTURE.holdMs + 40, 1);
    expect(pad.take()).toEqual(["button:up"]);
  });

  it("never grabs once the pointer has moved, even when the finger rests", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.move(50, [1, 120, 100]);
    pad.move(60, [1, 121, 100]);
    pad.until(GESTURE.holdMs * 3);
    expect(pad.take()).toEqual(["move", "move"]);
    expect(pad.phase).toBe("move");
  });

  it("ignores a wake-up of an earlier touch", () => {
    const step = trackpadDown(trackpadIdle(), [{ id: 1, x: 0, y: 0 }], 0);
    const later = trackpadDown(
      trackpadUp(step.state, [{ id: 1, x: 0, y: 0 }], 400).state,
      [{ id: 2, x: 300, y: 300 }],
      450,
    );
    const woke = trackpadWake(later.state, step.state.sequence, 1000);
    expect(woke.actions).toEqual([]);
  });
});

describe("trackpad: double tap", () => {
  it("double-clicks with two quick taps", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.up(80, 1);
    pad.down(200, 2, 104, 98);
    pad.up(270, 2);
    expect(pad.take()).toEqual([
      "click:left",
      "haptic:click",
      "button:down",
      "button:up",
      "haptic:click",
    ]);
  });

  it("drags when the second touch slides", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.up(80, 1);
    pad.take();
    pad.down(200, 2, 100, 100);
    expect(pad.take()).toEqual(["button:down"]);
    pad.move(230, [2, 103, 100]);
    expect(pad.take()).toEqual([]);
    pad.move(260, [2, 160, 100]);
    expect(pad.take()).toEqual(["haptic:grab", "move"]);
    pad.up(400, 2);
    expect(pad.take()).toEqual(["button:up"]);
  });

  it("keeps the button held while the second touch rests", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.up(80, 1);
    pad.down(200, 2, 100, 100);
    pad.take();
    pad.until(200 + GESTURE.grabMs);
    expect(pad.take()).toEqual(["haptic:grab"]);
    pad.up(1500, 2);
    expect(pad.take()).toEqual(["button:up"]);
  });

  it("does not press for a touch that comes late or far from the tap", () => {
    const late = trackpad();
    late.down(0, 1, 100, 100);
    late.up(80, 1);
    late.take();
    late.down(80 + GESTURE.doubleTapMs + 20, 2, 100, 100);
    expect(late.take()).toEqual([]);

    const far = trackpad();
    far.down(0, 1, 100, 100);
    far.up(80, 1);
    far.take();
    far.down(150, 2, 100 + GESTURE.doubleTapSlop + 10, 100);
    expect(far.take()).toEqual([]);
  });

  it("does not continue a double tap after a move", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.move(30, [1, 130, 100]);
    pad.up(60, 1);
    pad.down(100, 2, 130, 100);
    expect(pad.take()).toEqual(["move"]);
  });
});

describe("trackpad: two fingers", () => {
  it("right-clicks with a two-finger tap", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.down(40, 2, 160, 100);
    pad.up(120, 1);
    pad.up(130, 2);
    expect(pad.take()).toEqual(["click:right", "haptic:click"]);
    expect(pad.phase).toBe("idle");
  });

  it("scrolls when two fingers move together and never moves the pointer", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.down(30, 2, 160, 100);
    pad.move(60, [1, 100, 120], [2, 160, 120]);
    pad.move(80, [1, 100, 140], [2, 160, 140]);
    expect(pad.take()).toEqual(["scroll", "scroll"]);
    pad.up(100, 1);
    // The finger left behind does nothing until it lifts.
    pad.move(120, [2, 220, 200]);
    pad.up(140, 2);
    expect(pad.take()).toEqual([]);
  });

  it("does not right-click when the fingers pinch", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.down(20, 2, 160, 100);
    pad.move(60, [1, 85, 100], [2, 175, 100]);
    pad.up(100, 1);
    expect(pad.take()).toEqual([]);
  });
});

describe("trackpad: cancellation", () => {
  it("releases a held button when the touch is taken away", () => {
    const pad = trackpad();
    pad.down(0, 1, 100, 100);
    pad.until(GESTURE.holdMs);
    pad.take();
    pad.cancel();
    expect(pad.take()).toEqual(["button:up"]);
    expect(pad.phase).toBe("idle");
  });
});

/** Drives the screen machine. */
function screen() {
  let state: ScreenState = screenIdle();
  let actions: ScreenAction[] = [];
  const record = (step: { state: ScreenState; actions: ScreenAction[] }) => {
    state = step.state;
    actions.push(...step.actions);
  };
  const touches = (list: [number, number, number][]) =>
    list.map(([id, x, y]) => ({ id, x, y }));
  return {
    down: (now: number, ...list: [number, number, number][]) =>
      record(screenDown(state, touches(list), now)),
    move: (now: number, ...list: [number, number, number][]) =>
      record(screenMove(state, touches(list), now)),
    up: (now: number, ...list: [number, number, number][]) =>
      record(screenUp(state, touches(list), now)),
    take() {
      const taken = actions;
      actions = [];
      return taken;
    },
    get phase() {
      return state.phase;
    },
  };
}

describe("screen gestures", () => {
  it("taps where the finger landed", () => {
    const view = screen();
    view.down(0, [1, 50, 60]);
    view.move(30, [1, 52, 61]);
    view.up(80, [1, 52, 61]);
    expect(view.take()).toEqual([
      { type: "stop" },
      { type: "tap", x: 50, y: 60 },
    ]);
  });

  it("pans with one finger without tapping", () => {
    const view = screen();
    view.down(0, [1, 50, 60]);
    view.move(20, [1, 70, 60]);
    view.move(40, [1, 80, 60]);
    view.up(300, [1, 80, 60]);
    const taken = view.take();
    expect(taken.filter((action) => action.type === "tap")).toEqual([]);
    const panned = taken.reduce(
      (sum, action) => (action.type === "pan" ? sum + action.dx : sum),
      0,
    );
    expect(panned).toBeCloseTo(30 - GESTURE.slop);
    expect(taken.at(-1)).toEqual({ type: "settle" });
  });

  it("flings a pan released while moving", () => {
    const view = screen();
    view.down(0, [1, 0, 0]);
    for (let index = 1; index <= 10; index += 1)
      view.move(index * 16, [1, index * 12, 0]);
    view.up(165, [1, 120, 0]);
    const fling = view.take().at(-1);
    expect(fling?.type).toBe("fling");
    if (fling?.type === "fling") expect(fling.vx).toBeGreaterThan(0.4);
  });

  it("zooms in and back out around the pinch midpoint", () => {
    const view = screen();
    view.down(0, [1, 100, 100]);
    view.down(10, [2, 200, 100]);
    view.move(30, [1, 50, 100], [2, 250, 100]);
    view.move(60, [1, 100, 100], [2, 200, 100]);
    const zooms = view
      .take()
      .filter((action) => action.type === "zoom")
      .map((action) => (action.type === "zoom" ? action.scale : 0));
    expect(zooms[0]).toBeCloseTo(2);
    expect(zooms[1]).toBeCloseTo(0.5);
    expect(zooms[0] * zooms[1]).toBeCloseTo(1);
  });

  it("keeps panning with the finger left after a pinch", () => {
    const view = screen();
    view.down(0, [1, 100, 100]);
    view.down(10, [2, 200, 100]);
    view.move(30, [1, 90, 100], [2, 210, 100]);
    view.up(50, [2, 210, 100]);
    expect(view.phase).toBe("pan");
    view.take();
    view.move(70, [1, 80, 100]);
    expect(view.take()).toEqual([{ type: "pan", dx: -10, dy: 0 }]);
    view.up(400, [1, 80, 100]);
    expect(view.take()).toEqual([{ type: "settle" }]);
    expect(view.phase).toBe("idle");
  });
});
