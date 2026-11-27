/**
 * Visual references synchronized with a voice recording, shared by the
 * webapp (workspace images and the desktop) and the mobile app (images, text
 * files and the screen of an open app). Clients observe pointers and capture
 * frames; this module keeps the rules: limits, frame cadence, which pointer
 * samples are worth keeping, coordinates in target units and the manifest the
 * server stores next to the transcript (`backend/apps/harness/voice_schemas.py`).
 * `visualRecorder.ts` combines them into one recording session.
 */
import {
  VOICE_MAX_DURATION_MS,
  VOICE_MAX_POINTERS,
  voiceRecordingDuration,
} from "./voiceInput";

/**
 * What a pointer refers to: original image pixels, a recorded frame's pixels
 * or a text file. A file is a grid of characters: `width` is its longest
 * line's length, `height` its number of lines, and a pointer's `x`/`y` are a
 * zero-based column and line.
 */
export interface PointerTarget {
  kind: "image" | "desktop" | "app" | "file";
  path: string;
  width: number;
  height: number;
}

/**
 * What lies under a finger: the control of an app, as its accessibility tree
 * tells, or the line of a file (`role` `line`, `text` its content).
 */
export interface PointerElement {
  /** E.g. `button`, `text`, `image`, `textfield`, `line`. */
  role?: string;
  /** Accessibility label. */
  label?: string;
  /** Visible text, shortened. */
  text?: string;
  /** React Native `testID`. */
  test_id?: string;
  /** Native view class, e.g. `ReactTextView`. */
  type?: string;
  /** The element (or an ancestor) reacts to taps. */
  interactive?: boolean;
  /** `[x, y, width, height]` in frame pixels. */
  bounds?: [number, number, number, number];
}

export type PointerEventName = "move" | "down" | "up" | "leave";

/**
 * How a finger met an app: `use` reached the app, `point` only showed
 * something (the companion's pointer mode keeps it from the app).
 */
export type PointerMode = "use" | "point";

/** One sampled position or discrete pointer event. */
export interface PointerSample {
  t_ms: number;
  event: PointerEventName;
  x: number | null;
  y: number | null;
  target: PointerTarget | null;
  mode?: PointerMode;
  element?: PointerElement;
}

/** A frame captured at a recording-relative time. */
export interface RecordingFrame {
  t_ms: number;
  path: string;
  /** Visible texts of an app screen, in reading order. */
  texts?: string[];
}

/** Something an open app reported while it was recorded. */
export interface AppRecordingEvent {
  t_ms: number;
  kind: "log" | "error" | "network";
  level?: "debug" | "info" | "warn" | "error";
  message?: string;
  method?: string;
  /** Without query values. */
  url?: string;
  status?: number;
  duration_ms?: number;
}

/** The app an app recording shows. */
export interface AppRecordingContext {
  app_id: string;
  name: string;
  version?: string;
  platform: "android" | "ios";
}

/** `[x, y, width, height]` in target units (pixels; columns and lines). */
export type TargetRegion = [number, number, number, number];

/** What part of an image or file was on screen from `t_ms` on. */
export interface RecordingView {
  t_ms: number;
  /** Null once the user went back from it, e.g. to the chat. */
  target: PointerTarget | null;
  /** The visible part; the whole target when absent. */
  region?: TargetRegion;
  /** Estimated from a rendered preview's scroll position (Markdown, tables). */
  approximate?: boolean;
}

/** Everything visual a recording collected; null-free once finished. */
export interface PointingResult {
  pointers: PointerSample[];
  frames: RecordingFrame[];
  views?: RecordingView[];
  events?: AppRecordingEvent[];
  app?: AppRecordingContext;
}

/** Transcript fields the server stores with a recording. */
export interface RecordingTranscript {
  text: string;
  raw_text?: string;
  segments: Array<{
    start_ms: number;
    end_ms: number;
    text: string;
    raw_text?: string;
  }>;
  timing: "provider" | "approximate";
  cleanup_warning?: string;
  speech_model?: string;
  cleanup_model?: string;
}

/** Shortest pause between two frames while something happens. */
export const VISUAL_FRAME_INTERVAL_MS = 500;
/**
 * The same for an app's screen: frames follow its touches (each press and,
 * this long after its release, the result), so the cadence only catches
 * what the app changes by itself.
 */
export const VISUAL_APP_FRAME_INTERVAL_MS = 1_000;
export const VISUAL_PRESS_RESULT_MS = 700;
/**
 * A press's own frame counts if it was captured this soon after the press;
 * later it may already show the press's result, and the frame on screen
 * before the press is the better reference.
 */
export const VISUAL_PRESS_FRAME_MS = 500;
/** Every this long without speech or pointer activity doubles the pause. */
export const VISUAL_IDLE_BACKOFF_STEP_MS = 5_000;
export const VISUAL_MAX_FRAME_INTERVAL_MS = 30_000;
/** Pointer positions are sampled this often. */
export const VISUAL_SAMPLE_INTERVAL_MS = 100;
export const VISUAL_MAX_FRAME_BYTES = 2 * 1024 * 1024;
/** Frames are uploaded as they come; this bounds one recording. */
export const VISUAL_MAX_TOTAL_BYTES = 256 * 1024 * 1024;
/** Uploads waiting at once before a recording gives up as too slow. */
export const VISUAL_MAX_PENDING_UPLOADS = 8;
/** Keep aligned with `VoiceRecordingIn.frames` (server). */
export const VISUAL_MAX_FRAMES = 1200;
/** Keep aligned with `VoiceRecordingIn.events` (server). */
export const VISUAL_MAX_APP_EVENTS = 2000;
/** Keep aligned with `VoiceRecordingIn.views` (server). */
export const VISUAL_MAX_VIEWS = 1000;
/** A view that changes again within this time (scrolling) is replaced. */
export const VISUAL_VIEW_SETTLE_MS = 600;
/** Visible texts kept per frame, and their length. */
export const VISUAL_MAX_FRAME_TEXTS = 40;
export const VISUAL_MAX_TEXT_LENGTH = 120;
/** Log messages are cut to this length. */
export const VISUAL_MAX_MESSAGE_LENGTH = 1_000;

/** Where a recording's manifest and frames live in the workspace. */
export function recordingDirectory(recordingId: string): string {
  return `/workspace/.opencuria/recordings/${recordingId}`;
}

export function recordingFramesDirectory(recordingId: string): string {
  return `${recordingDirectory(recordingId)}/frames`;
}

/** File name of the `index`-th frame, e.g. `000003.jpg`. */
export function recordingFrameName(index: number): string {
  return `${String(index).padStart(6, "0")}.jpg`;
}

export function recordingFramePath(recordingId: string, index: number): string {
  return `${recordingFramesDirectory(recordingId)}/${recordingFrameName(index)}`;
}

/** Pause before the next frame after `idleMs` without activity. */
export function frameCaptureInterval(
  idleMs: number,
  base = VISUAL_FRAME_INTERVAL_MS,
): number {
  const steps = Math.max(0, Math.floor(idleMs / VISUAL_IDLE_BACKOFF_STEP_MS));
  return Math.min(
    VISUAL_MAX_FRAME_INTERVAL_MS,
    base * 2 ** Math.min(steps, 16),
  );
}

/**
 * When the next frame is due: soon after activity (speech, a pointer that
 * moved, a tap), ever less often while nothing happens. `base` is the
 * shortest interval.
 */
export class FrameCadence {
  private lastActivity: number;
  private next = -Infinity;

  constructor(
    now: number,
    private readonly base = VISUAL_FRAME_INTERVAL_MS,
  ) {
    this.lastActivity = now;
  }

  /** Something happened; a frame follows within the shortest interval. */
  activity(now: number): void {
    this.lastActivity = now;
    this.next = Math.min(this.next, now + this.base);
  }

  /** Whether a frame should be captured now. */
  due(now: number): boolean {
    return now >= this.next;
  }

  /** A capture starts now; the next one waits for the current interval. */
  captured(now: number): void {
    this.next = now + frameCaptureInterval(now - this.lastActivity, this.base);
  }

  /** Capture no later than at `at`, e.g. to show a tap's result. */
  dueBy(at: number): void {
    this.next = Math.min(this.next, at);
  }

  /** Capture at the next opportunity, e.g. after the viewed surface changed. */
  reset(): void {
    this.next = -Infinity;
  }
}

/** The frame on screen at `t`: the latest captured at or before it. */
export function frameAt<T extends { t_ms: number }>(
  frames: readonly T[],
  t: number,
): T | undefined {
  // Usually the latest frame: walk backwards without copying the history.
  for (let index = frames.length - 1; index >= 0; index--)
    if (frames[index]!.t_ms <= t) return frames[index];
  return undefined;
}

function sameTarget(
  a: PointerTarget | null | undefined,
  b: PointerTarget | null | undefined,
): boolean {
  return (
    a?.kind === b?.kind &&
    a?.path === b?.path &&
    a?.width === b?.width &&
    a?.height === b?.height
  );
}

/**
 * Pointer samples worth keeping. A point stays valid until the next event, so
 * unchanged moves and activity away from any target are dropped; presses,
 * releases and the first leave always stay.
 */
export class PointerTimeline {
  private readonly samples: PointerSample[] = [];

  constructor(private readonly limit = VOICE_MAX_POINTERS) {}

  get length(): number {
    return this.samples.length;
  }

  /** Whether any sample points at an image, the desktop or an app screen. */
  get engaged(): boolean {
    return this.samples.some((sample) => sample.target !== null);
  }

  /**
   * Add a sample. `skipped` when it adds nothing, `full` when the limit is
   * reached (the recording then has to stop pointing).
   */
  add(sample: PointerSample): "added" | "skipped" | "full" {
    if (sample.t_ms < 0 || sample.t_ms > VOICE_MAX_DURATION_MS)
      return "skipped";
    const previous = this.samples[this.samples.length - 1];
    const samePoint =
      previous?.x === sample.x &&
      previous?.y === sample.y &&
      previous?.mode === sample.mode &&
      sameTarget(previous?.target, sample.target);
    if (
      (!sample.target && !previous?.target) ||
      (samePoint && sample.event === "move")
    )
      return "skipped";
    if (this.samples.length >= this.limit) return "full";
    this.samples.push(sample);
    return "added";
  }

  /** The samples in time order. */
  result(): PointerSample[] {
    return [...this.samples].sort((a, b) => a.t_ms - b.t_ms);
  }
}

/**
 * Map a point in a surface of `surface` size (e.g. an app's content in
 * device pixels) to pixels of a frame of `frame` size; null outside.
 */
export function framePoint(
  point: { x: number; y: number },
  surface: { width: number; height: number },
  frame: { width: number; height: number },
): { x: number; y: number } | null {
  if (
    surface.width <= 0 ||
    surface.height <= 0 ||
    frame.width <= 0 ||
    frame.height <= 0 ||
    !Number.isFinite(point.x) ||
    !Number.isFinite(point.y)
  )
    return null;
  const x = point.x / surface.width;
  const y = point.y / surface.height;
  if (x < 0 || x >= 1 || y < 0 || y >= 1) return null;
  return {
    x: Math.min(frame.width - 1, Math.floor(x * frame.width)),
    y: Math.min(frame.height - 1, Math.floor(y * frame.height)),
  };
}

/** `[x, y, width, height]` of a surface rectangle in frame pixels, clipped. */
export function frameBounds(
  bounds: readonly [number, number, number, number],
  surface: { width: number; height: number },
  frame: { width: number; height: number },
): [number, number, number, number] | undefined {
  if (surface.width <= 0 || surface.height <= 0) return undefined;
  const scaleX = frame.width / surface.width;
  const scaleY = frame.height / surface.height;
  const left = Math.max(0, Math.round(bounds[0] * scaleX));
  const top = Math.max(0, Math.round(bounds[1] * scaleY));
  const right = Math.min(
    frame.width,
    Math.round((bounds[0] + bounds[2]) * scaleX),
  );
  const bottom = Math.min(
    frame.height,
    Math.round((bounds[1] + bounds[3]) * scaleY),
  );
  if (right <= left || bottom <= top) return undefined;
  return [left, top, right - left, bottom - top];
}

export interface Size {
  width: number;
  height: number;
}

/** A rectangle in screen points: `x`/`y` its top left corner. */
export interface Rect extends Size {
  x: number;
  y: number;
}

/**
 * `box` scaled about its center, then moved by the offset: a zoomed and
 * panned view as transforms draw it.
 */
export function zoomedRect(
  box: Rect,
  zoom: { scale: number; offsetX: number; offsetY: number },
): Rect {
  const width = box.width * zoom.scale;
  const height = box.height * zoom.scale;
  return {
    x: box.x + (box.width - width) / 2 + zoom.offsetX,
    y: box.y + (box.height - height) / 2 + zoom.offsetY,
    width,
    height,
  };
}

/** Where an image of `natural` size shows inside `box`, fit to contain. */
export function containedRect(box: Rect, natural: Size): Rect | null {
  if (
    box.width <= 0 ||
    box.height <= 0 ||
    natural.width <= 0 ||
    natural.height <= 0
  )
    return null;
  const fit = Math.min(box.width / natural.width, box.height / natural.height);
  const width = natural.width * fit;
  const height = natural.height * fit;
  return {
    x: box.x + (box.width - width) / 2,
    y: box.y + (box.height - height) / 2,
    width,
    height,
  };
}

/** The pixel of an image drawn into `rect` under a point; null outside. */
export function imagePixel(
  point: { x: number; y: number },
  rect: Rect,
  natural: Size,
): { x: number; y: number } | null {
  return framePoint(
    { x: point.x - rect.x, y: point.y - rect.y },
    rect,
    natural,
  );
}

/**
 * The pixels of an image drawn into `rect` that a `viewport` (from 0,0)
 * shows; undefined when it shows the whole image or none of it.
 */
export function visibleImageRegion(
  rect: Rect,
  viewport: Size,
  natural: Size,
): TargetRegion | undefined {
  if (rect.width <= 0 || rect.height <= 0) return undefined;
  const scaleX = natural.width / rect.width;
  const scaleY = natural.height / rect.height;
  const left = Math.max(0, Math.floor((0 - rect.x) * scaleX));
  const top = Math.max(0, Math.floor((0 - rect.y) * scaleY));
  const right = Math.min(
    natural.width,
    Math.ceil((viewport.width - rect.x) * scaleX),
  );
  const bottom = Math.min(
    natural.height,
    Math.ceil((viewport.height - rect.y) * scaleY),
  );
  if (right <= left || bottom <= top) return undefined;
  if (
    left === 0 &&
    top === 0 &&
    right === natural.width &&
    bottom === natural.height
  )
    return undefined;
  return [left, top, right - left, bottom - top];
}

/**
 * The lines of a file a rendered preview (Markdown, a table) shows, estimated
 * from its scroll position: `[0, first, width, count]` in file units, or
 * undefined while it shows everything or is not measured.
 */
export function previewLines(
  scroll: { offset: number; viewport: number; content: number },
  lineCount: number,
  width = Number.MAX_SAFE_INTEGER,
): TargetRegion | undefined {
  const { offset, viewport, content } = scroll;
  if (content <= 0 || viewport <= 0 || lineCount <= 0) return undefined;
  if (viewport >= content) return undefined;
  const top = Math.min(1, Math.max(0, offset / content));
  const bottom = Math.min(1, Math.max(top, (offset + viewport) / content));
  const first = Math.min(lineCount - 1, Math.floor(top * lineCount));
  const last = Math.min(
    lineCount,
    Math.max(first + 1, Math.ceil(bottom * lineCount)),
  );
  return [0, first, width, last - first];
}

/**
 * A canonical path inside `/workspace`, which the server accepts as a
 * recording's image or file: no `.`/`..` parts, backslashes or controls.
 */
export function isWorkspacePath(
  path: string | null | undefined,
): path is string {
  return (
    !!path &&
    path.startsWith("/workspace/") &&
    !/[\\\u0000-\u001f]/.test(path) &&
    !path
      .split("/")
      .some((part, index) => index > 0 && ["", ".", ".."].includes(part))
  );
}

function clip(value: unknown, length: number): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.replace(/\s+/g, " ").trim();
  return text ? text.slice(0, length) : undefined;
}

/** An element description within the server's limits; undefined when empty. */
export function pointerElement(
  element: Partial<Record<keyof PointerElement, unknown>> | null | undefined,
): PointerElement | undefined {
  if (!element) return undefined;
  const result: PointerElement = {};
  const role = clip(element.role, 40);
  const label = clip(element.label, VISUAL_MAX_TEXT_LENGTH);
  const text = clip(element.text, VISUAL_MAX_TEXT_LENGTH);
  const testId = clip(element.test_id, 120);
  const type = clip(element.type, 80);
  if (role) result.role = role;
  if (label) result.label = label;
  if (text) result.text = text;
  if (testId) result.test_id = testId;
  if (type) result.type = type;
  if (typeof element.interactive === "boolean")
    result.interactive = element.interactive;
  const bounds = element.bounds;
  if (
    Array.isArray(bounds) &&
    bounds.length === 4 &&
    bounds.every((value) => Number.isFinite(value) && value >= 0)
  )
    result.bounds = bounds.map(Math.round) as [number, number, number, number];
  return Object.keys(result).length ? result : undefined;
}

/** Visible texts of a frame: unique, shortened and bounded. */
export function frameTexts(
  texts: readonly unknown[] | null | undefined,
): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of texts ?? []) {
    const text = clip(value, VISUAL_MAX_TEXT_LENGTH);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    result.push(text);
    if (result.length >= VISUAL_MAX_FRAME_TEXTS) break;
  }
  return result;
}

/** Strip query values and fragments from a URL an app requested. */
export function redactedUrl(url: string): string {
  const [withoutFragment] = url.split("#", 1);
  const [base, query] = (withoutFragment ?? "").split("?", 2);
  if (query === undefined) return (base ?? "").slice(0, 500);
  const keys = query
    .split("&")
    .map((pair) => pair.split("=", 1)[0])
    .filter(Boolean)
    .map((key) => `${key}=…`);
  return `${base}?${keys.join("&")}`.slice(0, 500);
}

/** An app event within the server's limits; null when it carries nothing. */
export function appRecordingEvent(
  event: Partial<Record<keyof AppRecordingEvent, unknown>>,
): AppRecordingEvent | null {
  const t = Number(event.t_ms);
  if (!Number.isFinite(t) || t < 0 || t > VOICE_MAX_DURATION_MS) return null;
  const kind =
    event.kind === "log" || event.kind === "error" || event.kind === "network"
      ? event.kind
      : null;
  if (!kind) return null;
  const result: AppRecordingEvent = { t_ms: Math.round(t), kind };
  const level = event.level;
  if (
    level === "debug" ||
    level === "info" ||
    level === "warn" ||
    level === "error"
  )
    result.level = level;
  const message = clip(event.message, VISUAL_MAX_MESSAGE_LENGTH);
  if (message) result.message = message;
  if (kind === "network") {
    const method = clip(event.method, 12);
    if (method) result.method = method.toUpperCase();
    if (typeof event.url === "string" && event.url)
      result.url = redactedUrl(event.url);
    const status = Number(event.status);
    if (Number.isInteger(status) && status >= 0 && status < 1000)
      result.status = status;
    const duration = Number(event.duration_ms);
    if (Number.isFinite(duration) && duration >= 0)
      result.duration_ms = Math.round(duration);
    if (!result.url) return null;
  } else if (!message) return null;
  return result;
}

/**
 * The manifest saved for a recording (`POST /voice/recordings/`). Frames and
 * events beyond the limits are dropped, newest last.
 */
export function visualRecordingInput(input: {
  recordingId: string;
  durationMs: number;
  transcript: RecordingTranscript;
  result: PointingResult;
}): RecordingTranscript & {
  recording_id: string;
  duration_ms: number;
  pointers: PointerSample[];
  frames: RecordingFrame[];
  views: RecordingView[];
  events: AppRecordingEvent[];
  app?: AppRecordingContext;
} {
  const { transcript, result } = input;
  const duration = voiceRecordingDuration(input.durationMs);
  // The server takes whole milliseconds; native clocks (iOS) report fractions.
  const within = <T extends { t_ms: number }>(items: readonly T[]) =>
    items
      .map((item) => ({ ...item, t_ms: Math.round(item.t_ms) }))
      .filter((item) => item.t_ms >= 0 && item.t_ms <= duration)
      .sort((a, b) => a.t_ms - b.t_ms);
  const frames = within(result.frames).slice(0, VISUAL_MAX_FRAMES);
  const framePaths = new Set(frames.map((frame) => frame.path));
  // A pointer on a recorded surface needs its frame; images and files
  // stand alone.
  const pointers = within(result.pointers).filter(
    (pointer) =>
      !pointer.target ||
      pointer.target.kind === "image" ||
      pointer.target.kind === "file" ||
      framePaths.has(pointer.target.path),
  );
  return {
    text: transcript.text,
    raw_text: transcript.raw_text ?? "",
    segments: transcript.segments.filter(
      (segment) => segment.end_ms <= duration,
    ),
    timing: transcript.timing,
    cleanup_warning: transcript.cleanup_warning ?? "",
    speech_model: transcript.speech_model ?? "",
    cleanup_model: transcript.cleanup_model ?? "",
    recording_id: input.recordingId,
    duration_ms: duration,
    pointers,
    frames,
    views: within(result.views ?? []).slice(0, VISUAL_MAX_VIEWS),
    events: within(result.events ?? []).slice(0, VISUAL_MAX_APP_EVENTS),
    ...(result.app ? { app: result.app } : {}),
  };
}
