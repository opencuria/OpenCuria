/**
 * One recording's visual references: the surfaces the user shows while
 * talking, in turn. Clients announce what is on screen (`show`); the newest
 * shown surface that is still open is the active one and the only one that
 * records. Images and text files never need screenshots: pointers land in
 * image pixels or on file lines, and views say which part was visible.
 * Frame surfaces (an app's screen) capture frames through a port, only while
 * they are on screen, after presses and ever less often while nothing
 * happens. The result is `null` when nothing was shown, so a plain dictation
 * stays one.
 */
import {
  FrameCadence,
  PointerTimeline,
  VISUAL_APP_FRAME_INTERVAL_MS,
  VISUAL_MAX_APP_EVENTS,
  VISUAL_MAX_FRAMES,
  VISUAL_MAX_VIEWS,
  VISUAL_PRESS_FRAME_MS,
  VISUAL_PRESS_RESULT_MS,
  VISUAL_SAMPLE_INTERVAL_MS,
  VISUAL_VIEW_SETTLE_MS,
  appRecordingEvent,
  frameAt,
  frameBounds,
  framePoint,
  frameTexts,
  isWorkspacePath,
  pointerElement,
  recordingFramePath,
  type AppRecordingContext,
  type AppRecordingEvent,
  type PointerElement,
  type PointerEventName,
  type PointerMode,
  type PointerSample,
  type PointerTarget,
  type PointingResult,
  type RecordingFrame,
  type RecordingView,
  type Size,
  type TargetRegion,
} from "./visualRecording";

/** A frame a port captured; `t_ms` counts from the recording's start. */
export interface CapturedFrame {
  /** Identifies the pixels; the next capture compares against it. */
  id: string;
  width: number;
  height: number;
  t_ms: number;
  /** Visible texts in reading order. */
  texts?: readonly unknown[];
  /** Where the client keeps the image until it is uploaded. */
  source?: string;
}

/** Captures a frame surface; answers `unchanged` for the same pixels. */
export type FrameCapture = (
  index: number,
  previousId: string | null,
) => Promise<CapturedFrame | { unchanged: true }>;

export type VisualSurfaceSpec =
  /** A workspace image in its original pixels. */
  | { kind: "image"; path: string; width: number; height: number }
  /**
   * A workspace text file. `approximate`: a rendered preview (Markdown, a
   * table) whose visible lines are estimated; it takes no pointers.
   */
  | {
      kind: "file";
      path: string;
      lines: readonly string[];
      approximate?: boolean;
    }
  /** A screen recorded as frames (an open app), in its own input units. */
  | { kind: "app"; size: Size; capture: FrameCapture };

/** A finger or mouse on a surface. */
export interface SurfacePointer {
  event: Exclude<PointerEventName, "leave">;
  /**
   * Image pixels, a file's zero-based column (`x`) and line (`y`), or a frame
   * surface's input units; null away from the target.
   */
  point: { x: number; y: number } | null;
  mode: PointerMode;
  /** Recording time; now when absent. */
  t_ms?: number;
  /** The control under the finger; frame surfaces map its bounds. */
  element?: PointerElement;
}

export interface VisualSurface {
  readonly kind: VisualSurfaceSpec["kind"];
  /** On top: what the user sees, and the only surface that records. */
  readonly active: boolean;
  /** Fingers can point at it (images, source text, apps). */
  readonly pointable: boolean;
  /**
   * The visible part in target units; the whole target when absent. The
   * first call records the surface's view; later ones are kept once they
   * settle, so scrolling leaves one entry.
   */
  view(region?: TargetRegion): void;
  pointer(input: SurfacePointer): void;
  /** The surface closed or went out of sight. */
  hide(): void;
}

/** A log line, error or request; `t_ms` may lie before the recording. */
export type VisualEvent = Partial<Record<keyof AppRecordingEvent, unknown>> & {
  t_ms: number;
};

export interface VisualRecorderOptions {
  recordingId: string;
  /** Milliseconds since the recording (its audio) started. */
  now: () => number;
}

interface Pending {
  run: Promise<void>;
}

/** Larger images are no recording target (the server's limit). */
const MAX_IMAGE_SIDE = 32_768;

/** A workspace image or file the server accepts as a recording's target. */
function acceptable(
  spec: Extract<VisualSurfaceSpec, { kind: "image" | "file" }>,
): boolean {
  if (!isWorkspacePath(spec.path)) return false;
  if (spec.kind === "file") return true;
  return [spec.width, spec.height].every(
    (side) => Number.isFinite(side) && side >= 1 && side <= MAX_IMAGE_SIDE,
  );
}

const sameRegion = (a?: TargetRegion, b?: TargetRegion): boolean =>
  a === b || (!!a && !!b && a.every((value, index) => value === b[index]));

const sameTarget = (a: PointerTarget | null, b: PointerTarget | null) =>
  a?.kind === b?.kind &&
  a?.path === b?.path &&
  a?.width === b?.width &&
  a?.height === b?.height;

abstract class Surface implements VisualSurface {
  abstract readonly kind: VisualSurfaceSpec["kind"];
  abstract readonly pointable: boolean;
  /** The last view the client reported; recorded again when it returns. */
  region: TargetRegion | undefined;
  /** A view was reported; until then nobody knows what part shows. */
  measured = false;
  hidden = false;
  /** A press runs; leaving the surface ends it. */
  pressed = false;
  /** Time of the last pointer sample; moves come at most this often. */
  lastMove = -Infinity;

  constructor(protected readonly recorder: VisualRecorder) {}

  get active(): boolean {
    return this.recorder.top === this;
  }

  view(region?: TargetRegion): void {
    if (this.hidden) return;
    this.region = region;
    this.measured = true;
    if (this.active) this.recordView();
  }

  abstract pointer(input: SurfacePointer): void;

  hide(): void {
    if (this.hidden) return;
    this.hidden = true;
    this.recorder.remove(this);
  }

  /** Became the active surface. */
  abstract activated(): void;

  /** Another surface covered it, or it closed. */
  deactivated(): void {
    if (this.pressed) this.recorder.leave();
    this.pressed = false;
  }

  /** Track the press; false for a move too soon after the last one. */
  protected throttle(input: SurfacePointer, t: number): boolean {
    if (input.event === "down") this.pressed = true;
    if (input.event === "up") this.pressed = false;
    if (input.event === "move" && t - this.lastMove < VISUAL_SAMPLE_INTERVAL_MS)
      return false;
    this.lastMove = t;
    return true;
  }

  protected recordView(): void {
    /* Only images and files have views. */
  }
}

/** An image or a text file: no frames, coordinates in target units. */
class TargetSurface extends Surface {
  readonly kind: "image" | "file";
  readonly pointable: boolean;
  readonly target: PointerTarget;
  private readonly lines: readonly string[] | null;
  private readonly approximate: boolean;

  constructor(
    recorder: VisualRecorder,
    spec: Extract<VisualSurfaceSpec, { kind: "image" | "file" }>,
  ) {
    super(recorder);
    this.kind = spec.kind;
    if (spec.kind === "image") {
      this.lines = null;
      this.approximate = false;
      this.target = {
        kind: "image",
        path: spec.path,
        width: Math.round(spec.width),
        height: Math.round(spec.height),
      };
    } else {
      this.lines = spec.lines;
      this.approximate = Boolean(spec.approximate);
      // A loop: spreading a large file's lines would overflow the stack.
      let width = 1;
      for (const line of spec.lines)
        if (line.length > width) width = line.length;
      this.target = {
        kind: "file",
        path: spec.path,
        width,
        height: Math.max(1, spec.lines.length),
      };
    }
    this.pointable = !this.approximate;
  }

  activated(): void {
    // A file shows lines only once they are measured; never "all of it".
    if (this.measured) this.recordView();
  }

  protected recordView(): void {
    this.recorder.view({
      target: this.target,
      region: this.clamp(this.region),
      ...(this.approximate ? { approximate: true } : {}),
    });
  }

  pointer(input: SurfacePointer): void {
    if (this.hidden || !this.active || !this.pointable) return;
    const t = input.t_ms ?? this.recorder.now();
    if (!this.throttle(input, t)) return;
    const point = this.inside(input.point);
    const line =
      point && this.lines && input.event === "down"
        ? pointerElement({ role: "line", text: this.lines[point.y] })
        : undefined;
    this.recorder.sample({
      t_ms: t,
      event: input.event,
      x: point?.x ?? null,
      y: point?.y ?? null,
      target: point ? this.target : null,
      mode: input.mode,
      ...(line ? { element: line } : {}),
    });
  }

  private inside(
    point: { x: number; y: number } | null,
  ): { x: number; y: number } | null {
    if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y))
      return null;
    const x = Math.floor(point.x);
    const y = Math.floor(point.y);
    if (this.kind === "file")
      // A finger beside a short line still points at that line.
      return y >= 0 && y < this.target.height
        ? { x: Math.min(Math.max(0, x), this.target.width - 1), y }
        : null;
    return x >= 0 && x < this.target.width && y >= 0 && y < this.target.height
      ? { x, y }
      : null;
  }

  private clamp(region?: TargetRegion): TargetRegion | undefined {
    if (!region || !region.every(Number.isFinite)) return undefined;
    const left = Math.max(0, Math.floor(region[0]));
    const top = Math.max(0, Math.floor(region[1]));
    const right = Math.min(this.target.width, Math.ceil(region[0] + region[2]));
    const bottom = Math.min(
      this.target.height,
      Math.ceil(region[1] + region[3]),
    );
    if (right <= left || bottom <= top) return undefined;
    if (
      left === 0 &&
      top === 0 &&
      right === this.target.width &&
      bottom === this.target.height
    )
      return undefined;
    return [left, top, right - left, bottom - top];
  }
}

/** An app's screen: frames on show, on presses and by cadence. */
class FrameSurface extends Surface {
  readonly kind = "app" as const;
  readonly pointable = true;
  private cadence: FrameCadence;
  /** Touches in arrival order; a press waits for its frame. */
  private touches: Promise<void> = Promise.resolve();

  constructor(
    recorder: VisualRecorder,
    private readonly spec: Extract<VisualSurfaceSpec, { kind: "app" }>,
  ) {
    super(recorder);
    this.cadence = new FrameCadence(
      recorder.now(),
      VISUAL_APP_FRAME_INTERVAL_MS,
    );
  }

  get capture(): FrameCapture {
    return this.spec.capture;
  }

  activated(): void {
    // What the user returns to is captured at once.
    this.cadence = new FrameCadence(
      this.recorder.now(),
      VISUAL_APP_FRAME_INTERVAL_MS,
    );
    void this.recorder.captureFrame(this);
  }

  tick(now: number): void {
    if (this.cadence.due(now)) void this.recorder.captureFrame(this);
  }

  captured(now: number): void {
    this.cadence.captured(now);
  }

  settled(): Promise<void> {
    return this.touches;
  }

  pointer(input: SurfacePointer): void {
    if (this.hidden || !this.active) return;
    const t = input.t_ms ?? this.recorder.now();
    if (!this.throttle(input, t)) return;
    this.touches = this.touches.then(() =>
      this.touch(input, t).catch(() => undefined),
    );
  }

  private async touch(input: SurfacePointer, t: number): Promise<void> {
    const now = this.recorder.now();
    this.cadence.activity(now);
    // The press refers to what was on screen: capture it first.
    if (input.event === "down") await this.recorder.captureFrame(this);
    if (input.event === "up") this.cadence.dueBy(now + VISUAL_PRESS_RESULT_MS);
    if (this.hidden || this.recorder.closed) return;
    const before = this.recorder.frameAt(t);
    let shown = before;
    if (input.event === "down") {
      // The frame captured for the press, unless it came too late and may
      // show the press's result already.
      const captured = this.recorder.frameAt(Number.MAX_SAFE_INTEGER);
      if (
        captured &&
        (!before || captured.frame.t_ms - t <= VISUAL_PRESS_FRAME_MS)
      )
        shown = captured;
    }
    shown ??= this.recorder.frameAt(Number.MAX_SAFE_INTEGER);
    const point =
      shown && input.point
        ? framePoint(input.point, this.spec.size, shown.size)
        : null;
    const target: PointerTarget | null =
      shown && point
        ? {
            kind: "app",
            path: shown.frame.path,
            width: shown.size.width,
            height: shown.size.height,
          }
        : null;
    const element =
      input.event === "down" && input.element
        ? pointerElement({
            ...input.element,
            bounds:
              input.element.bounds && shown
                ? frameBounds(input.element.bounds, this.spec.size, shown.size)
                : undefined,
          })
        : undefined;
    this.recorder.sample({
      t_ms: t,
      event: input.event,
      x: target ? point!.x : null,
      y: target ? point!.y : null,
      target,
      mode: input.mode,
      ...(element ? { element } : {}),
    });
  }
}

/**
 * The visual side of one recording. Feed it surfaces, pointers, views and app
 * events while the audio records; `finish` freezes it into the manifest data.
 */
export class VisualRecorder {
  readonly recordingId: string;
  readonly now: () => number;
  private readonly surfaces: Surface[] = [];
  private readonly pointers = new PointerTimeline();
  private readonly frames: RecordingFrame[] = [];
  private readonly frameSizes = new Map<string, Size>();
  private readonly sources: string[] = [];
  private readonly views: RecordingView[] = [];
  private readonly events: AppRecordingEvent[] = [];
  private readonly listeners = new Set<() => void>();
  private previousFrameId: string | null = null;
  private capturing: Pending | null = null;
  private shown = false;
  private appShown = false;
  private app: AppRecordingContext | undefined;
  private pointersFull = false;
  /** Pointer samples since the last view; a view they refer to stays. */
  private samplesSinceView = 0;
  private current: Surface | null = null;
  closed = false;

  constructor(options: VisualRecorderOptions) {
    this.recordingId = options.recordingId;
    this.now = options.now;
  }

  /** The active surface: the newest shown that is still open. */
  get top(): Surface | null {
    return this.current;
  }

  /** Something was shown; the recording keeps a visual timeline. */
  get engaged(): boolean {
    return this.shown;
  }

  /** Calls `listener` when the active surface changes. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Show a surface on top of the others; invalid workspace paths show nothing. */
  show(spec: VisualSurfaceSpec): VisualSurface {
    const surface =
      spec.kind === "app"
        ? new FrameSurface(this, spec)
        : new TargetSurface(this, spec);
    if (this.closed || (spec.kind !== "app" && !acceptable(spec))) {
      surface.hidden = true;
      return surface;
    }
    this.surfaces.push(surface);
    this.shown = true;
    if (spec.kind === "app") this.appShown = true;
    this.activate();
    return surface;
  }

  /** The app the recording shows; kept once its screen was shown. */
  setApp(app: AppRecordingContext): void {
    this.app = app;
  }

  /** An app's log line, error or request; earlier ones keep their age. */
  event(input: VisualEvent): void {
    if (this.closed || this.events.length >= VISUAL_MAX_APP_EVENTS) return;
    const before = -input.t_ms;
    const event = appRecordingEvent({
      ...input,
      t_ms: Math.max(0, input.t_ms),
      message:
        before > 0 && typeof input.message === "string" && input.message
          ? `(${(before / 1000).toFixed(1)} s before recording) ${input.message}`
          : input.message,
    });
    if (event) this.events.push(event);
  }

  /** Call regularly (about every 100 ms): frame surfaces capture when due. */
  tick(): void {
    if (this.closed) return;
    const top = this.current;
    if (top instanceof FrameSurface) top.tick(this.now());
  }

  /** Stop recording; the frame on screen is captured, pending work settles. */
  async finish(): Promise<{
    result: PointingResult | null;
    sources: string[];
  }> {
    const top = this.current;
    if (!this.closed && top instanceof FrameSurface)
      await this.captureFrame(top);
    await Promise.all(
      this.surfaces
        .filter((surface) => surface instanceof FrameSurface)
        .map((surface) => (surface as FrameSurface).settled()),
    );
    await this.capturing?.run;
    this.close();
    return { result: this.result(), sources: [...this.sources] };
  }

  /** Discard; returns the client's frame files to delete. */
  cancel(): string[] {
    this.close();
    return [...this.sources];
  }

  private close(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.current) this.current.deactivated();
    this.current = null;
    this.emit();
  }

  private result(): PointingResult | null {
    if (!this.shown) return null;
    return {
      pointers: this.pointers.result(),
      frames: [...this.frames],
      views: [...this.views],
      events: this.appShown ? [...this.events] : [],
      ...(this.appShown && this.app ? { app: this.app } : {}),
    };
  }

  // Internal API of the surfaces.

  /** @internal */
  remove(surface: Surface): void {
    const index = this.surfaces.indexOf(surface);
    if (index >= 0) this.surfaces.splice(index, 1);
    if (this.current === surface) this.activate();
  }

  /** @internal Leave the active surface's press. */
  leave(): void {
    this.sample({
      t_ms: this.now(),
      event: "leave",
      x: null,
      y: null,
      target: null,
    });
  }

  /** @internal */
  sample(sample: PointerSample): void {
    if (this.closed || this.pointersFull) return;
    const added = this.pointers.add(sample);
    if (added === "full") this.pointersFull = true;
    else if (added === "added") this.samplesSinceView += 1;
  }

  /** @internal */
  view(view: Omit<RecordingView, "t_ms">): void {
    if (this.closed) return;
    const t = this.now();
    const last = this.views[this.views.length - 1];
    if (
      last &&
      sameTarget(last.target, view.target) &&
      sameRegion(last.region, view.region) &&
      Boolean(last.approximate) === Boolean(view.approximate)
    )
      return;
    const entry: RecordingView = {
      t_ms: t,
      target: view.target,
      ...(view.region ? { region: view.region } : {}),
      ...(view.approximate ? { approximate: true } : {}),
    };
    // Scrolling: the view settles into one entry, unless a pointer used it.
    if (
      last &&
      view.target &&
      sameTarget(last.target, view.target) &&
      t - last.t_ms < VISUAL_VIEW_SETTLE_MS &&
      this.samplesSinceView === 0
    ) {
      this.views[this.views.length - 1] = entry;
      return;
    }
    if (this.views.length >= VISUAL_MAX_VIEWS) return;
    this.views.push(entry);
    this.samplesSinceView = 0;
  }

  /** @internal The frame on screen at `t` with its size. */
  frameAt(t: number): { frame: RecordingFrame; size: Size } | null {
    const frame = frameAt(this.frames, t);
    const size = frame && this.frameSizes.get(frame.path);
    return frame && size ? { frame, size } : null;
  }

  /** @internal Capture the frame surface now, or join the running capture. */
  captureFrame(surface: FrameSurface): Promise<void> {
    if (this.capturing) return this.capturing.run;
    if (this.closed || this.frames.length >= VISUAL_MAX_FRAMES)
      return Promise.resolve();
    const index = this.frames.length;
    surface.captured(this.now());
    const pending: Pending = {
      run: surface
        .capture(index, this.previousFrameId)
        .then((frame) => {
          if (this.closed || "unchanged" in frame) return;
          const path = recordingFramePath(this.recordingId, index);
          this.frames.push({
            t_ms: Math.max(0, frame.t_ms),
            path,
            texts: frameTexts(frame.texts),
          });
          this.frameSizes.set(path, {
            width: frame.width,
            height: frame.height,
          });
          if (frame.source) this.sources.push(frame.source);
          this.previousFrameId = frame.id;
        })
        .catch(() => {
          // A frame that failed only leaves a gap in the timeline.
        })
        .finally(() => {
          if (this.capturing === pending) this.capturing = null;
        }),
    };
    this.capturing = pending;
    return pending.run;
  }

  private activate(): void {
    const next = this.surfaces[this.surfaces.length - 1] ?? null;
    const previous = this.current;
    if (next === previous) return;
    previous?.deactivated();
    this.current = next;
    // Back from an image or file to the chat or an app.
    if (previous instanceof TargetSurface && !(next instanceof TargetSurface))
      this.view({ target: null });
    next?.activated();
    this.emit();
  }

  private emit(): void {
    for (const listener of [...this.listeners]) listener();
  }
}
