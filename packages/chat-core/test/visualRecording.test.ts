import { describe, expect, it } from "vitest";
import {
  FrameCadence,
  PointerTimeline,
  VISUAL_FRAME_INTERVAL_MS,
  VISUAL_MAX_FRAME_INTERVAL_MS,
  VISUAL_MAX_FRAME_TEXTS,
  appRecordingEvent,
  containedRect,
  frameAt,
  frameBounds,
  frameCaptureInterval,
  framePoint,
  frameTexts,
  imagePixel,
  isWorkspacePath,
  pointerElement,
  previewLines,
  recordingFramePath,
  redactedUrl,
  visibleImageRegion,
  visualRecordingInput,
  zoomedRect,
  type PointerSample,
  type PointerTarget,
} from "../src/visualRecording";

const ID = "8d4d1b3e-5a59-4f39-9d9c-0c3c8a1b2f10";
const frame = (index: number): PointerTarget => ({
  kind: "app",
  path: recordingFramePath(ID, index),
  width: 540,
  height: 1200,
});

describe("visual recordings", () => {
  it("names frames inside the recording's workspace folder", () => {
    expect(recordingFramePath(ID, 3)).toBe(
      `/workspace/.opencuria/recordings/${ID}/frames/000003.jpg`,
    );
  });

  it("captures often while something happens and ever less while idle", () => {
    expect(frameCaptureInterval(0)).toBe(VISUAL_FRAME_INTERVAL_MS);
    expect(frameCaptureInterval(5_000)).toBe(VISUAL_FRAME_INTERVAL_MS * 2);
    expect(frameCaptureInterval(10 * 60_000)).toBe(
      VISUAL_MAX_FRAME_INTERVAL_MS,
    );
    const cadence = new FrameCadence(0);
    expect(cadence.due(0)).toBe(true);
    cadence.captured(0);
    expect(cadence.due(499)).toBe(false);
    expect(cadence.due(500)).toBe(true);
    cadence.captured(12_000);
    // Twelve idle seconds: two backoff steps.
    expect(cadence.due(13_999)).toBe(false);
    cadence.activity(13_000);
    expect(cadence.due(13_500)).toBe(true);
    cadence.captured(13_500);
    cadence.reset();
    expect(cadence.due(13_501)).toBe(true);
  });

  it("finds the frame on screen at a time", () => {
    const frames = [
      { t_ms: 0, path: "a" },
      { t_ms: 400, path: "b" },
      { t_ms: 900, path: "c" },
    ];
    expect(frameAt(frames, 399)?.path).toBe("a");
    expect(frameAt(frames, 400)?.path).toBe("b");
    expect(frameAt(frames, 5_000)?.path).toBe("c");
    expect(frameAt([{ t_ms: 10, path: "x" }], 5)).toBeUndefined();
  });

  it("keeps presses and changes but drops idle and untargeted samples", () => {
    const timeline = new PointerTimeline(4);
    const at = (
      t_ms: number,
      event: PointerSample["event"],
      x: number | null,
      target: PointerTarget | null = frame(0),
    ): PointerSample => ({
      t_ms,
      event,
      x,
      y: x,
      target,
      mode: "use",
    });
    expect(timeline.add(at(0, "move", null, null))).toBe("skipped");
    expect(timeline.add(at(100, "down", 10))).toBe("added");
    expect(timeline.add(at(200, "move", 10))).toBe("skipped");
    expect(timeline.add(at(300, "move", 20))).toBe("added");
    expect(timeline.add(at(400, "up", 20))).toBe("added");
    expect(timeline.engaged).toBe(true);
    expect(timeline.add(at(500, "leave", null, null))).toBe("added");
    expect(timeline.add(at(600, "move", null, null))).toBe("skipped");
    expect(timeline.add(at(700, "down", 30))).toBe("full");
    expect(timeline.result().map((sample) => sample.t_ms)).toEqual([
      100, 300, 400, 500,
    ]);
  });

  it("maps surface coordinates into frame pixels", () => {
    const surface = { width: 1080, height: 2400 };
    const size = { width: 540, height: 1200 };
    expect(framePoint({ x: 540, y: 1200 }, surface, size)).toEqual({
      x: 270,
      y: 600,
    });
    expect(framePoint({ x: 1080, y: 10 }, surface, size)).toBeNull();
    expect(framePoint({ x: Number.NaN, y: 1 }, surface, size)).toBeNull();
    expect(frameBounds([100, 200, 300, 4000], surface, size)).toEqual([
      50, 100, 150, 1100,
    ]);
    expect(frameBounds([2000, 0, 10, 10], surface, size)).toBeUndefined();
  });

  it("bounds element descriptions, screen texts and app events", () => {
    expect(
      pointerElement({
        role: "button",
        text: `  Add\n item ${"x".repeat(300)}`,
        test_id: "add-button",
        interactive: true,
        bounds: [1.4, 2, 3, 4],
        label: 42,
      }),
    ).toEqual({
      role: "button",
      text: `Add item ${"x".repeat(111)}`,
      test_id: "add-button",
      interactive: true,
      bounds: [1, 2, 3, 4],
    });
    expect(pointerElement({ bounds: [-1, 0, 1, 1] })).toBeUndefined();
    expect(frameTexts(["Todos", "Todos", " ", 3, "Add"])).toEqual([
      "Todos",
      "Add",
    ]);
    expect(
      frameTexts(Array.from({ length: 99 }, (_, index) => `#${index}`)),
    ).toHaveLength(VISUAL_MAX_FRAME_TEXTS);
    expect(
      redactedUrl("http://api.opencuria.local:8080/a?token=s3cret&x=1#f"),
    ).toBe("http://api.opencuria.local:8080/a?token=…&x=…");
    expect(
      appRecordingEvent({
        t_ms: 12.6,
        kind: "network",
        method: "get",
        url: "http://api.opencuria.local/todos?key=1",
        status: 500,
        duration_ms: 120.2,
      }),
    ).toEqual({
      t_ms: 13,
      kind: "network",
      method: "GET",
      url: "http://api.opencuria.local/todos?key=…",
      status: 500,
      duration_ms: 120,
    });
    expect(
      appRecordingEvent({ t_ms: 1, kind: "log", message: " " }),
    ).toBeNull();
    expect(
      appRecordingEvent({ t_ms: -1, kind: "log", message: "x" }),
    ).toBeNull();
    expect(
      appRecordingEvent({ t_ms: 1, kind: "log", level: "warn", message: "Hi" }),
    ).toEqual({ t_ms: 1, kind: "log", level: "warn", message: "Hi" });
  });

  it("builds the manifest within the recording and keeps pointers on kept frames", () => {
    const input = visualRecordingInput({
      recordingId: ID,
      durationMs: 1_000.4,
      transcript: {
        text: "Make it green.",
        segments: [{ start_ms: 0, end_ms: 900, text: "Make it green." }],
        timing: "provider",
      },
      result: {
        pointers: [
          {
            t_ms: 500,
            event: "down",
            x: 1,
            y: 1,
            target: frame(1),
            mode: "point",
          },
          { t_ms: 20, event: "down", x: 1, y: 1, target: frame(0) },
          { t_ms: 2_000, event: "up", x: 1, y: 1, target: frame(0) },
          { t_ms: 30, event: "down", x: 1, y: 1, target: frame(7) },
        ],
        frames: [
          { t_ms: 400, path: frame(1).path, texts: ["Todos"] },
          { t_ms: 0, path: frame(0).path },
          { t_ms: 3_000, path: frame(2).path },
        ],
        events: [{ t_ms: 600, kind: "log", level: "info", message: "Added" }],
        app: { app_id: "a", name: "Todo Demo", platform: "android" },
      },
    });
    expect(input.duration_ms).toBe(1_000);
    expect(input.frames.map((item) => item.t_ms)).toEqual([0, 400]);
    expect(input.pointers.map((item) => item.t_ms)).toEqual([20, 500]);
    expect(input.events).toHaveLength(1);
    expect(input.app?.name).toBe("Todo Demo");
    expect(input.cleanup_warning).toBe("");
  });

  it("sends whole milliseconds for sub-millisecond native times", () => {
    const input = visualRecordingInput({
      recordingId: ID,
      durationMs: 1_000,
      transcript: { text: "", segments: [], timing: "approximate" },
      result: {
        pointers: [
          { t_ms: 400.6, event: "down", x: 1, y: 1, target: frame(0) },
          { t_ms: 999.7, event: "up", x: 1, y: 1, target: frame(0) },
          { t_ms: 1_000.6, event: "up", x: 1, y: 1, target: frame(0) },
        ],
        frames: [{ t_ms: 0.4, path: frame(0).path }],
        events: [{ t_ms: 12.5, kind: "log", message: "Added" }],
      },
    });
    expect(input.frames.map((item) => item.t_ms)).toEqual([0]);
    expect(input.pointers.map((item) => item.t_ms)).toEqual([401, 1_000]);
    expect(input.events.map((item) => item.t_ms)).toEqual([13]);
  });
});

describe("visual geometry", () => {
  it("finds image pixels under a finger on a zoomed, contained picture", () => {
    const page = { x: 0, y: 0, width: 400, height: 800 };
    const natural = { width: 1600, height: 800 };
    const rect = containedRect(page, natural)!;
    expect(rect).toEqual({ x: 0, y: 300, width: 400, height: 200 });
    expect(imagePixel({ x: 100, y: 350 }, rect, natural)).toEqual({
      x: 400,
      y: 200,
    });
    expect(imagePixel({ x: 100, y: 200 }, rect, natural)).toBeNull();
    // Zoomed 2x about the center and moved right by 100 points.
    const zoomed = zoomedRect(rect, { scale: 2, offsetX: 100, offsetY: 0 });
    expect(zoomed).toEqual({ x: -100, y: 200, width: 800, height: 400 });
    expect(imagePixel({ x: 300, y: 400 }, zoomed, natural)).toEqual({
      x: 800,
      y: 400,
    });
    expect(visibleImageRegion(zoomed, page, natural)).toEqual([
      200, 0, 800, 800,
    ]);
    expect(visibleImageRegion(rect, page, natural)).toBeUndefined();
    expect(containedRect(page, { width: 0, height: 10 })).toBeNull();
  });

  it("estimates the lines a scrolled preview shows", () => {
    const scroll = { offset: 500, viewport: 250, content: 1000 };
    expect(previewLines(scroll, 200, 80)).toEqual([0, 100, 80, 50]);
    expect(previewLines({ ...scroll, offset: 900 }, 200)).toEqual([
      0,
      180,
      Number.MAX_SAFE_INTEGER,
      20,
    ]);
    // Everything fits, or nothing is measured yet.
    expect(
      previewLines({ offset: 0, viewport: 800, content: 600 }, 50),
    ).toBeUndefined();
    expect(
      previewLines({ offset: 0, viewport: 0, content: 600 }, 50),
    ).toBeUndefined();
  });

  it("accepts only canonical workspace paths", () => {
    expect(isWorkspacePath("/workspace/src/app.ts")).toBe(true);
    expect(isWorkspacePath("/workspace/../etc/passwd")).toBe(false);
    expect(isWorkspacePath("/workspace/a//b")).toBe(false);
    expect(isWorkspacePath("/tmp/a.png")).toBe(false);
    expect(isWorkspacePath("/workspace/a\\b")).toBe(false);
    expect(isWorkspacePath(undefined)).toBe(false);
  });

  it("keeps views and file pointers in the manifest", () => {
    const target: PointerTarget = {
      kind: "file",
      path: "/workspace/src/app.ts",
      width: 80,
      height: 120,
    };
    const input = visualRecordingInput({
      recordingId: ID,
      durationMs: 3000,
      transcript: { text: "Here", segments: [], timing: "approximate" },
      result: {
        pointers: [
          { t_ms: 1200.4, event: "down", x: 3, y: 16, target, mode: "point" },
        ],
        frames: [],
        views: [
          { t_ms: 100, target, region: [0, 10, 80, 30] },
          { t_ms: 5000, target: null },
        ],
      },
    });
    expect(input.pointers).toHaveLength(1);
    expect(input.views).toEqual([
      { t_ms: 100, target, region: [0, 10, 80, 30] },
    ]);
  });
});
