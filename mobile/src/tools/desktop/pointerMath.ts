/**
 * Pointer and viewport math of the desktop panel. All functions are worklets:
 * gestures run them on the UI thread so the native cursor follows the finger
 * without waiting for JavaScript.
 *
 * Coordinates: the framebuffer (`fb`) is the remote desktop in pixels. The
 * screen box shows it at `box` points; `zoom` ≥ 1 enlarges the content to
 * `box * zoom` and `panX/panY` is the visible window's offset into it. The
 * viewer page keeps one layout size (`viewerPageSize`); zoom only scales it.
 */

export interface Size {
  width: number;
  height: number;
}

/** Largest zoom: one framebuffer pixel per point, at least 2×, at most 6×. */
export function maxZoomFor(fb: Size, box: Size): number {
  "worklet";
  if (box.width <= 0) return 1;
  return Math.min(6, Math.max(2, fb.width / box.width));
}

export function clamp(value: number, min: number, max: number): number {
  "worklet";
  return Math.min(max, Math.max(min, value));
}

/**
 * Pointer acceleration like a laptop trackpad: slow strokes are precise,
 * fast flicks cross the screen. `speed` is the finger speed in points/ms.
 */
export function pointerGain(speed: number): number {
  "worklet";
  const t = clamp((speed - 0.05) / (1.4 - 0.05), 0, 1);
  const eased = t * t * (3 - 2 * t);
  return 0.6 + eased * 1.9;
}

/**
 * Move the pointer by a finger delta (points). Movement is defined on the
 * visible screen, so zooming in automatically makes the pointer finer.
 */
export function movePointer(
  pointer: { x: number; y: number },
  delta: { x: number; y: number },
  speed: number,
  fb: Size,
  box: Size,
  zoom: number,
): { x: number; y: number } {
  "worklet";
  if (box.width <= 0 || box.height <= 0) return pointer;
  const gain = pointerGain(speed);
  const perPoint = fb.width / (box.width * zoom);
  return {
    x: clamp(pointer.x + delta.x * gain * perPoint, 0, fb.width - 1),
    y: clamp(pointer.y + delta.y * gain * perPoint, 0, fb.height - 1),
  };
}

/** The pan range of a zoomed view. */
export function clampPan(
  pan: { x: number; y: number },
  box: Size,
  zoom: number,
): { x: number; y: number } {
  "worklet";
  return {
    x: clamp(pan.x, 0, Math.max(0, box.width * zoom - box.width)),
    y: clamp(pan.y, 0, Math.max(0, box.height * zoom - box.height)),
  };
}

/** Where the pointer appears inside the screen box, in points. */
export function pointerOnScreen(
  pointer: { x: number; y: number },
  fb: Size,
  box: Size,
  zoom: number,
  pan: { x: number; y: number },
): { x: number; y: number } {
  "worklet";
  return {
    x: (pointer.x / fb.width) * box.width * zoom - pan.x,
    y: (pointer.y / fb.height) * box.height * zoom - pan.y,
  };
}

/** The framebuffer pixel under a point of the screen box. */
export function screenToFramebuffer(
  point: { x: number; y: number },
  fb: Size,
  box: Size,
  zoom: number,
  pan: { x: number; y: number },
): { x: number; y: number } {
  "worklet";
  if (box.width <= 0 || box.height <= 0) return { x: 0, y: 0 };
  return {
    x: clamp(
      ((point.x + pan.x) / (box.width * zoom)) * fb.width,
      0,
      fb.width - 1,
    ),
    y: clamp(
      ((point.y + pan.y) / (box.height * zoom)) * fb.height,
      0,
      fb.height - 1,
    ),
  };
}

/** Keep the pointer inside the visible window (with a margin) while zoomed. */
export function followPointer(
  pointer: { x: number; y: number },
  fb: Size,
  box: Size,
  zoom: number,
  pan: { x: number; y: number },
  margin = 28,
): { x: number; y: number } {
  "worklet";
  if (zoom <= 1) return { x: 0, y: 0 };
  const at = pointerOnScreen(pointer, fb, box, zoom, pan);
  const marginX = Math.min(margin, box.width / 4);
  const marginY = Math.min(margin, box.height / 4);
  let x = pan.x;
  let y = pan.y;
  if (at.x < marginX) x -= marginX - at.x;
  else if (at.x > box.width - marginX) x += at.x - (box.width - marginX);
  if (at.y < marginY) y -= marginY - at.y;
  else if (at.y > box.height - marginY) y += at.y - (box.height - marginY);
  return clampPan({ x, y }, box, zoom);
}

/** Zoom to `next` while keeping the content under `focal` (box points) fixed. */
export function zoomAround(
  zoom: number,
  next: number,
  pan: { x: number; y: number },
  focal: { x: number; y: number },
  box: Size,
): { x: number; y: number } {
  "worklet";
  const ratio = next / zoom;
  return clampPan(
    {
      x: (pan.x + focal.x) * ratio - focal.x,
      y: (pan.y + focal.y) * ratio - focal.y,
    },
    box,
    next,
  );
}

/**
 * Layout size of the viewer page, in the box's aspect ratio. Zooming only
 * scales this page natively, so it never re-lays out the WebView. One device
 * pixel per framebuffer pixel is all the detail the remote screen has; the
 * page is at least the box and at most twice its width, so a fitted view is
 * never scaled down far enough to shimmer.
 */
export function viewerPageSize(fb: Size, box: Size, pixelRatio: number): Size {
  "worklet";
  if (fb.width <= 0 || box.width <= 0 || box.height <= 0 || pixelRatio <= 0)
    return { width: box.width, height: box.height };
  const width = clamp(fb.width / pixelRatio, box.width, box.width * 2);
  return { width, height: (width * box.height) / box.width };
}

/** The largest box of the framebuffer's aspect ratio inside `area`. */
export function fitBox(fb: Size, area: Size): Size {
  "worklet";
  if (fb.width <= 0 || fb.height <= 0 || area.width <= 0 || area.height <= 0)
    return { width: 0, height: 0 };
  const scale = Math.min(area.width / fb.width, area.height / fb.height);
  return {
    width: Math.floor(fb.width * scale),
    height: Math.floor(fb.height * scale),
  };
}
