import { describe, expect, it } from "vitest";
import {
  clampPan,
  fitBox,
  followPointer,
  maxZoomFor,
  movePointer,
  pointerGain,
  pointerOnScreen,
  screenToFramebuffer,
  viewerPageSize,
  zoomAround,
} from "./pointerMath";

const fb = { width: 1920, height: 1080 };
const box = { width: 384, height: 216 };

describe("trackpad pointer", () => {
  it("accelerates fast strokes and keeps slow ones precise", () => {
    expect(pointerGain(0)).toBeCloseTo(0.6);
    expect(pointerGain(10)).toBeCloseTo(2.5);
    expect(pointerGain(0.4)).toBeGreaterThan(pointerGain(0.1));
  });

  it("moves in screen points, finer when zoomed, and stays on the desktop", () => {
    const slow = movePointer(
      { x: 960, y: 540 },
      { x: 10, y: 0 },
      0,
      fb,
      box,
      1,
    );
    expect(slow.x).toBeCloseTo(960 + 10 * 0.6 * 5);
    const zoomed = movePointer(
      { x: 960, y: 540 },
      { x: 10, y: 0 },
      0,
      fb,
      box,
      2,
    );
    expect(zoomed.x - 960).toBeCloseTo((slow.x - 960) / 2);
    expect(
      movePointer({ x: 10, y: 10 }, { x: -500, y: 5000 }, 5, fb, box, 1),
    ).toEqual({ x: 0, y: 1079 });
  });
});

describe("zoomed viewport", () => {
  it("maps between framebuffer pixels and screen points", () => {
    const pan = { x: 120, y: 40 };
    const at = pointerOnScreen({ x: 1000, y: 500 }, fb, box, 2.5, pan);
    const back = screenToFramebuffer(at, fb, box, 2.5, pan);
    expect(back.x).toBeCloseTo(1000);
    expect(back.y).toBeCloseTo(500);
  });

  it("zooms around the pinch focus", () => {
    const focal = { x: 100, y: 50 };
    const before = screenToFramebuffer(focal, fb, box, 1, { x: 0, y: 0 });
    const pan = zoomAround(1, 3, { x: 0, y: 0 }, focal, box);
    const after = screenToFramebuffer(focal, fb, box, 3, pan);
    expect(after.x).toBeCloseTo(before.x);
    expect(after.y).toBeCloseTo(before.y);
  });

  it("keeps the pan inside the content", () => {
    expect(clampPan({ x: -5, y: 9999 }, box, 2)).toEqual({ x: 0, y: 216 });
    expect(clampPan({ x: 50, y: 50 }, box, 1)).toEqual({ x: 0, y: 0 });
  });

  it("follows the pointer to the edge of a zoomed view", () => {
    const pan = followPointer({ x: 1500, y: 540 }, fb, box, 2, {
      x: 0,
      y: 108,
    });
    const at = pointerOnScreen({ x: 1500, y: 540 }, fb, box, 2, pan);
    expect(at.x).toBeCloseTo(box.width - 28);
    // At the content's edge the view stops panning.
    const edge = followPointer({ x: 1919, y: 540 }, fb, box, 2, pan);
    expect(edge.x).toBe(box.width);
    expect(followPointer({ x: 1900, y: 540 }, fb, box, 1, pan)).toEqual({
      x: 0,
      y: 0,
    });
  });

  it("limits zoom to about one framebuffer pixel per point", () => {
    expect(maxZoomFor(fb, box)).toBe(5);
    expect(maxZoomFor({ width: 600, height: 400 }, box)).toBe(2);
    expect(
      maxZoomFor({ width: 3840, height: 2160 }, { width: 300, height: 1 }),
    ).toBe(6);
  });

  it("fits the framebuffer's aspect ratio into the available area", () => {
    expect(fitBox(fb, { width: 336, height: 600 })).toEqual({
      width: 336,
      height: 189,
    });
    expect(fitBox(fb, { width: 1000, height: 100 })).toEqual({
      width: 177,
      height: 100,
    });
    expect(fitBox(fb, { width: 0, height: 100 })).toEqual({
      width: 0,
      height: 0,
    });
  });

  it("lays the viewer page out once at the framebuffer's detail", () => {
    // 3x phone: 1920 px fit into 640 points, one device pixel each.
    expect(viewerPageSize(fb, { width: 390, height: 219.375 }, 3)).toEqual({
      width: 640,
      height: 360,
    });
    // Never smaller than the box, never more than twice its width.
    expect(viewerPageSize(fb, box, 10).width).toBe(box.width);
    expect(viewerPageSize(fb, box, 1).width).toBe(box.width * 2);
  });
});
