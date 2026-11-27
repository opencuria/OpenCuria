import { describe, expect, it, vi } from "vitest";
import {
  VISUAL_APP_FRAME_INTERVAL_MS,
  VISUAL_PRESS_FRAME_MS,
  VISUAL_PRESS_RESULT_MS,
  VISUAL_VIEW_SETTLE_MS,
  recordingFramePath,
} from "../src/visualRecording";
import {
  VisualRecorder,
  type CapturedFrame,
  type FrameCapture,
} from "../src/visualRecorder";

const ID = "8d4d1b3e-5a59-4f39-9d9c-0c3c8a1b2f10";
const IMAGE = "/workspace/screens/login.png";
const FILE = "/workspace/src/cart.ts";
const LINES = [
  "export function total(items) {",
  "  return items.reduce((sum, item) => sum + item.price, 0);",
  "}",
];

/** Let running captures finish. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function recorder() {
  let time = 0;
  const visual = new VisualRecorder({ recordingId: ID, now: () => time });
  return {
    visual,
    at(ms: number) {
      time = ms;
    },
  };
}

/** A frame port whose pixels change when `screen` changes. */
function appScreen() {
  const state = { screen: "list", captures: 0, now: 0 };
  const capture = vi.fn<FrameCapture>(async (index, previousId) => {
    state.captures += 1;
    if (previousId === state.screen) return { unchanged: true };
    const frame: CapturedFrame = {
      id: state.screen,
      width: 540,
      height: 1200,
      t_ms: state.now,
      texts: [state.screen === "list" ? "Todos" : "Details"],
      source: `file:///cache/${index}.jpg`,
    };
    return frame;
  });
  return { state, capture };
}

describe("VisualRecorder", () => {
  it("stays a plain dictation while nothing is shown", async () => {
    const { visual } = recorder();
    visual.tick();
    expect(visual.engaged).toBe(false);
    expect(await visual.finish()).toEqual({ result: null, sources: [] });
  });

  it("points at image pixels and file lines without any screenshot", async () => {
    const { visual, at } = recorder();
    at(100);
    const image = visual.show({
      kind: "image",
      path: IMAGE,
      width: 1600,
      height: 800,
    });
    expect(image.active).toBe(true);
    // Shown, but not yet measured: no view until the host reports one.
    image.view();
    at(400);
    image.pointer({
      event: "down",
      point: { x: 600.7, y: 200.2 },
      mode: "point",
    });
    at(450);
    // Too soon after the last sample: dropped.
    image.pointer({ event: "move", point: { x: 610, y: 205 }, mode: "point" });
    at(600);
    image.pointer({ event: "up", point: { x: 640, y: 210 }, mode: "point" });
    at(1_000);
    image.hide();
    at(1_200);
    const file = visual.show({ kind: "file", path: FILE, lines: LINES });
    at(1_300);
    file.view([0, 0, 58, 2]);
    at(2_000);
    file.pointer({ event: "down", point: { x: 99, y: 1 }, mode: "use" });
    file.pointer({ event: "up", point: { x: 99, y: 1 }, mode: "use" });
    const { result, sources } = await visual.finish();
    expect(sources).toEqual([]);
    expect(result?.frames).toEqual([]);
    const imageTarget = {
      kind: "image",
      path: IMAGE,
      width: 1600,
      height: 800,
    };
    const fileTarget = { kind: "file", path: FILE, width: 58, height: 3 };
    expect(result?.views).toEqual([
      { t_ms: 100, target: imageTarget },
      { t_ms: 1_000, target: null },
      // The file's lines only once they were measured, never "all of it".
      { t_ms: 1_300, target: fileTarget, region: [0, 0, 58, 2] },
    ]);
    expect(result?.pointers).toEqual([
      {
        t_ms: 400,
        event: "down",
        x: 600,
        y: 200,
        target: imageTarget,
        mode: "point",
      },
      {
        t_ms: 600,
        event: "up",
        x: 640,
        y: 210,
        target: imageTarget,
        mode: "point",
      },
      {
        t_ms: 2_000,
        event: "down",
        // A finger beside a short line still points at it.
        x: 57,
        y: 1,
        target: fileTarget,
        mode: "use",
        element: {
          role: "line",
          text: "return items.reduce((sum, item) => sum + item.price, 0);",
        },
      },
      {
        t_ms: 2_000,
        event: "up",
        x: 57,
        y: 1,
        target: fileTarget,
        mode: "use",
      },
    ]);
    expect(result?.app).toBeUndefined();
  });

  it("keeps one view per scroll position and the views a pointer used", async () => {
    const { visual, at } = recorder();
    const file = visual.show({ kind: "file", path: FILE, lines: LINES });
    at(100);
    file.view([0, 0, 58, 1]);
    at(200);
    file.view([0, 1, 58, 1]);
    at(300);
    file.view([0, 1, 58, 2]);
    // The same view again adds nothing.
    at(1_500);
    file.view([0, 1, 58, 2]);
    file.pointer({ event: "down", point: { x: 0, y: 2 }, mode: "point" });
    at(1_600);
    file.view([0, 0, 58, 2]);
    at(1_600 + VISUAL_VIEW_SETTLE_MS + 1);
    file.view([0, 0, 58, 1]);
    const { result } = await visual.finish();
    expect(result?.views?.map((view) => [view.t_ms, view.region])).toEqual([
      [300, [0, 1, 58, 2]],
      [1_600, [0, 0, 58, 2]],
      [1_600 + VISUAL_VIEW_SETTLE_MS + 1, [0, 0, 58, 1]],
    ]);
  });

  it("records only the surface on top and returns to the one beneath", async () => {
    const { visual, at } = recorder();
    const file = visual.show({ kind: "file", path: FILE, lines: LINES });
    file.view([0, 0, 58, 2]);
    at(500);
    const image = visual.show({
      kind: "image",
      path: IMAGE,
      width: 1600,
      height: 800,
    });
    expect(file.active).toBe(false);
    // Beneath the image: neither views nor pointers count.
    file.view([0, 1, 58, 2]);
    file.pointer({ event: "down", point: { x: 0, y: 0 }, mode: "point" });
    image.view([0, 0, 800, 400]);
    at(2_000);
    image.hide();
    expect(file.active).toBe(true);
    const { result } = await visual.finish();
    expect(result?.pointers).toEqual([]);
    expect(
      result?.views?.map((view) => [
        view.t_ms,
        view.target?.kind ?? null,
        view.region,
      ]),
    ).toEqual([
      [0, "file", [0, 0, 58, 2]],
      [500, "image", [0, 0, 800, 400]],
      // Back on the file where it was scrolled to meanwhile.
      [2_000, "file", [0, 1, 58, 2]],
    ]);
  });

  it("takes no pointers on rendered previews and marks their views approximate", async () => {
    const { visual } = recorder();
    const preview = visual.show({
      kind: "file",
      path: "/workspace/README.md",
      lines: LINES,
      approximate: true,
    });
    expect(preview.pointable).toBe(false);
    preview.pointer({ event: "down", point: { x: 0, y: 0 }, mode: "point" });
    preview.view([0, 1, 58, 2]);
    const { result } = await visual.finish();
    expect(result?.pointers).toEqual([]);
    expect(result?.views).toEqual([
      {
        t_ms: 0,
        target: {
          kind: "file",
          path: "/workspace/README.md",
          width: 58,
          height: 3,
        },
        region: [0, 1, 58, 2],
        approximate: true,
      },
    ]);
  });

  it("handles files with very many lines", async () => {
    const { visual } = recorder();
    const lines = Array.from({ length: 300_000 }, (_, index) => `${index}`);
    lines[123_456] = "x".repeat(500);
    const file = visual.show({ kind: "file", path: FILE, lines });
    file.view([0, 100, 10, 40]);
    const { result } = await visual.finish();
    expect(result?.views?.[0]?.target).toMatchObject({
      width: 500,
      height: 300_000,
    });
  });

  it("ignores images larger than the server takes", async () => {
    const { visual } = recorder();
    visual.show({ kind: "image", path: IMAGE, width: 40_000, height: 10 });
    expect(visual.engaged).toBe(false);
  });

  it("ignores paths outside the workspace", async () => {
    const { visual } = recorder();
    const image = visual.show({
      kind: "image",
      path: "/workspace/../etc/shadow.png",
      width: 10,
      height: 10,
    });
    image.pointer({ event: "down", point: { x: 1, y: 1 }, mode: "point" });
    expect(visual.engaged).toBe(false);
    expect((await visual.finish()).result).toBeNull();
  });

  it("captures an app's screen on show, on presses and after their result, never while hidden", async () => {
    const { visual, at } = recorder();
    const { state, capture } = appScreen();
    visual.setApp({ app_id: "app-1", name: "Todo", platform: "ios" });
    const app = visual.show({
      kind: "app",
      size: { width: 1080, height: 2400 },
      capture,
    });
    await settle();
    expect(capture).toHaveBeenCalledTimes(1);
    at(300);
    state.now = 300;
    app.pointer({
      event: "down",
      point: { x: 540, y: 1200 },
      mode: "use",
      element: { role: "button", text: "Open", bounds: [0, 1100, 1080, 200] },
    });
    at(350);
    app.pointer({ event: "up", point: { x: 540, y: 1200 }, mode: "use" });
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(2));
    await settle();
    // The tap opened another screen; it shows after the press's result time.
    state.screen = "details";
    at(350 + VISUAL_PRESS_RESULT_MS - 1);
    visual.tick();
    expect(capture).toHaveBeenCalledTimes(2);
    at(350 + VISUAL_PRESS_RESULT_MS);
    state.now = 350 + VISUAL_PRESS_RESULT_MS;
    visual.tick();
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(3));
    await settle();
    // The chat covers the app: no frames meanwhile.
    const image = visual.show({
      kind: "image",
      path: IMAGE,
      width: 1600,
      height: 800,
    });
    at(60_000);
    visual.tick();
    app.pointer({ event: "down", point: { x: 1, y: 1 }, mode: "point" });
    expect(capture).toHaveBeenCalledTimes(3);
    image.hide();
    // Back on the app: captured at once; unchanged pixels add no frame.
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(4));
    visual.event({ t_ms: -2_500, kind: "log", level: "warn", message: "Slow" });
    visual.event({
      t_ms: 61_000,
      kind: "network",
      method: "get",
      url: "https://api.test/todos?token=x",
      status: 200,
    });
    const { result, sources } = await visual.finish();
    expect(sources).toEqual(["file:///cache/0.jpg", "file:///cache/1.jpg"]);
    expect(result?.frames).toEqual([
      { t_ms: 0, path: recordingFramePath(ID, 0), texts: ["Todos"] },
      {
        t_ms: 350 + VISUAL_PRESS_RESULT_MS,
        path: recordingFramePath(ID, 1),
        texts: ["Details"],
      },
    ]);
    const frame0 = {
      kind: "app",
      path: recordingFramePath(ID, 0),
      width: 540,
      height: 1200,
    };
    expect(result?.pointers).toEqual([
      {
        t_ms: 300,
        event: "down",
        x: 270,
        y: 600,
        target: frame0,
        mode: "use",
        element: { role: "button", text: "Open", bounds: [0, 550, 540, 100] },
      },
      { t_ms: 350, event: "up", x: 270, y: 600, target: frame0, mode: "use" },
    ]);
    expect(result?.events).toEqual([
      {
        t_ms: 0,
        kind: "log",
        level: "warn",
        message: "(2.5 s before recording) Slow",
      },
      {
        t_ms: 61_000,
        kind: "network",
        method: "GET",
        url: "https://api.test/todos?token=…",
        status: 200,
      },
    ]);
    expect(result?.app).toEqual({
      app_id: "app-1",
      name: "Todo",
      platform: "ios",
    });
  });

  it("keeps a press on the frame shown before it when its own came too late", async () => {
    const { visual, at } = recorder();
    const { state, capture } = appScreen();
    const app = visual.show({
      kind: "app",
      size: { width: 1080, height: 2400 },
      capture,
    });
    await settle();
    // A slow device: the press's frame is taken after the app reacted.
    state.screen = "details";
    state.now = 2_000 + VISUAL_PRESS_FRAME_MS + 1;
    at(2_000);
    app.pointer({ event: "down", point: { x: 540, y: 1200 }, mode: "use" });
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(2));
    await settle();
    // A fast one: the press's own frame shows what was pressed.
    state.screen = "list";
    at(5_000);
    state.now = 5_000 + 50;
    app.pointer({ event: "down", point: { x: 540, y: 1200 }, mode: "use" });
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(3));
    const { result } = await visual.finish();
    expect(
      result?.pointers
        .filter((pointer) => pointer.event === "down")
        .map((pointer) => pointer.target?.path),
    ).toEqual([recordingFramePath(ID, 0), recordingFramePath(ID, 2)]);
  });

  it("backs off while the app shows nothing new", async () => {
    const { visual, at } = recorder();
    const { capture } = appScreen();
    visual.show({ kind: "app", size: { width: 1080, height: 2400 }, capture });
    await settle();
    expect(capture).toHaveBeenCalledTimes(1);
    at(VISUAL_APP_FRAME_INTERVAL_MS - 1);
    visual.tick();
    expect(capture).toHaveBeenCalledTimes(1);
    at(VISUAL_APP_FRAME_INTERVAL_MS);
    visual.tick();
    await settle();
    expect(capture).toHaveBeenCalledTimes(2);
    // Thirty idle seconds: the interval has grown to its maximum.
    at(30_000);
    visual.tick();
    await settle();
    expect(capture).toHaveBeenCalledTimes(3);
    at(45_000);
    visual.tick();
    expect(capture).toHaveBeenCalledTimes(3);
    const { result } = await visual.finish();
    // Unchanged pixels: one frame for the whole recording.
    expect(result?.frames).toHaveLength(1);
  });

  it("leaves out the app and its logs when its screen was never shown", async () => {
    const { visual } = recorder();
    visual.setApp({ app_id: "app-1", name: "Todo", platform: "android" });
    visual.event({ t_ms: 10, kind: "error", message: "Boom" });
    visual.show({ kind: "image", path: IMAGE, width: 10, height: 10 });
    const { result } = await visual.finish();
    expect(result?.app).toBeUndefined();
    expect(result?.events).toEqual([]);
  });

  it("tells listeners which surface is on top and returns frame files on cancel", async () => {
    const { visual } = recorder();
    const { capture } = appScreen();
    const listener = vi.fn();
    visual.subscribe(listener);
    const app = visual.show({
      kind: "app",
      size: { width: 1080, height: 2400 },
      capture,
    });
    expect(visual.top?.kind).toBe("app");
    await vi.waitFor(() => expect(capture).toHaveBeenCalledTimes(1));
    app.hide();
    expect(visual.top).toBeNull();
    expect(listener).toHaveBeenCalledTimes(2);
    expect(visual.cancel()).toEqual(["file:///cache/0.jpg"]);
    // Closed: nothing records any more.
    visual.show({ kind: "image", path: IMAGE, width: 10, height: 10 });
    expect(visual.top).toBeNull();
  });
});
