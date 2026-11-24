import { requireOptionalNativeModule } from "expo-modules-core";
import type { PointerElement } from "@opencuria/chat-core/visualRecording";

/**
 * Which touches the companion layer over an open app takes: none (hidden,
 * the app gets everything), all (the sheet or pointer mode), or only those
 * inside `regions` (the recording bar; the app gets the rest). Regions are
 * the companion's own controls in every mode: recordings ignore their touches.
 */
export type CompanionTouchMode = "none" | "all" | "regions";

/** `[x, y, width, height]` in layout points of the companion's root view. */
export type CompanionRegion = [number, number, number, number];

/** The ways that open the companion over an app, shared by every app. */
export interface CompanionMenuTriggers {
  shake: boolean;
  threeFingerPress: boolean;
  button: boolean;
}

export type CompanionMenuTrigger = keyof CompanionMenuTriggers;

/** The app's content in device pixels; touches and bounds use these. */
export interface AppSurface {
  width: number;
  height: number;
  platform: "android" | "ios";
}

/** A frame of the app's content, saved as JPEG in OpenCuria's cache. */
export type AppFrame =
  | { unchanged: true; capturedAt: number }
  | {
      unchanged?: false;
      /** `file://` URI of the JPEG. */
      uri: string;
      width: number;
      height: number;
      /** Identifies the pixels; the next capture compares against it. */
      frameId: string;
      /** Epoch milliseconds. */
      capturedAt: number;
      /** Visible texts in reading order. */
      texts: string[];
    };

/** A finger on the app, observed while a recording runs. */
export interface AppTouchEvent {
  /** Epoch milliseconds. */
  t: number;
  phase: "down" | "move" | "up" | "cancel";
  /** Device pixels within the app's content. */
  x: number;
  y: number;
  /** `point` when the companion kept the touch from the app. */
  mode: "use" | "point";
  /** The control under the finger, on `down`; bounds in device pixels. */
  element?: PointerElement & { bounds?: [number, number, number, number] };
}

/** A log line, error or backend request of the open app. */
export interface AppEventRecord {
  /** Epoch milliseconds. */
  t: number;
  kind: "log" | "error" | "network";
  level?: "debug" | "info" | "warn" | "error";
  message?: string;
  method?: string;
  url?: string;
  status?: number;
  durationMs?: number;
}

interface Subscription {
  remove(): void;
}

interface CompanionModule {
  companionReady?(): void;
  /** `regions` flattened: `[x, y, width, height, …]`. */
  setCompanionTouchMode?(mode: CompanionTouchMode, regions: number[]): unknown;
  reloadOpenApp?(): Promise<void>;
  close?(): Promise<void>;
  /** A value on Android, a promise on iOS. */
  menuTriggers?(): CompanionMenuTriggers | Promise<CompanionMenuTriggers>;
  setMenuTrigger?(
    trigger: CompanionMenuTrigger,
    on: boolean,
  ): CompanionMenuTriggers | Promise<CompanionMenuTriggers>;
  startAppObservation?(recordingId: string): Promise<AppSurface>;
  captureAppFrame?(
    recordingId: string,
    index: number,
    previousFrameId: string | null,
  ): Promise<AppFrame>;
  stopAppObservation?(): Promise<void>;
  recentAppEvents?(since: number): Promise<AppEventRecord[]>;
  addListener?(
    event: "onCompanionOpen" | "onAppTouch" | "onAppEvent",
    listener: (payload: never) => void,
  ): Subscription;
}

const native = requireOptionalNativeModule<CompanionModule>(
  "OpenCuriaAppRuntime",
);

/** Whether this build can show the companion over open apps. */
export function isCompanionAvailable(): boolean {
  return typeof native?.setCompanionTouchMode === "function";
}

/** The companion's React tree is ready for commands. */
export function companionReady(): void {
  native?.companionReady?.();
}

export function setCompanionTouchMode(
  mode: CompanionTouchMode,
  regions: readonly CompanionRegion[] = [],
): void {
  const result = native?.setCompanionTouchMode?.(
    mode,
    regions.flatMap((region) => region.map((value) => Math.round(value))),
  );
  // Android answers with a promise; a closed app has nothing to update.
  if (result && typeof (result as Promise<void>).catch === "function")
    (result as Promise<void>).catch(() => undefined);
}

/** Start the open app again from its bundle. */
export async function reloadOpenApp(): Promise<void> {
  await native?.reloadOpenApp?.();
}

/** Close the open app; OpenCuria shows again. */
export async function closeOpenAppFromCompanion(): Promise<void> {
  await native?.close?.();
}

/** Every way is on until the device says otherwise. */
export const DEFAULT_MENU_TRIGGERS: CompanionMenuTriggers = {
  shake: true,
  threeFingerPress: true,
  button: true,
};

export async function menuTriggers(): Promise<CompanionMenuTriggers> {
  return (await native?.menuTriggers?.()) ?? DEFAULT_MENU_TRIGGERS;
}

/** Turn a way on or off; the last one stays on. Resolves to the new state. */
export async function setMenuTrigger(
  trigger: CompanionMenuTrigger,
  on: boolean,
): Promise<CompanionMenuTriggers> {
  return (await native?.setMenuTrigger?.(trigger, on)) ?? menuTriggers();
}

/** Observe the open app's touches, logs and requests for a recording. */
export async function startAppObservation(
  recordingId: string,
): Promise<AppSurface> {
  if (!native?.startAppObservation)
    throw new Error("This build of OpenCuria cannot record apps.");
  return native.startAppObservation(recordingId);
}

/** Capture the open app's content; `unchanged` when the pixels are the same. */
export async function captureAppFrame(
  recordingId: string,
  index: number,
  previousFrameId: string | null,
): Promise<AppFrame> {
  if (!native?.captureAppFrame)
    throw new Error("This build of OpenCuria cannot record apps.");
  return native.captureAppFrame(recordingId, index, previousFrameId);
}

export async function stopAppObservation(): Promise<void> {
  await native?.stopAppObservation?.();
}

/** Logs, errors and requests of the open app since `since` (epoch ms). */
export async function recentAppEvents(
  since: number,
): Promise<AppEventRecord[]> {
  return (await native?.recentAppEvents?.(since)) ?? [];
}

const noSubscription: Subscription = { remove: () => undefined };

/** Native asks to show the companion (shake, three fingers, menu button). */
export function onCompanionOpen(listener: () => void): Subscription {
  return (
    native?.addListener?.("onCompanionOpen", listener as never) ??
    noSubscription
  );
}

export function onAppTouch(
  listener: (event: AppTouchEvent) => void,
): Subscription {
  return (
    native?.addListener?.("onAppTouch", listener as never) ?? noSubscription
  );
}

export function onAppEvent(
  listener: (event: AppEventRecord) => void,
): Subscription {
  return (
    native?.addListener?.("onAppEvent", listener as never) ?? noSubscription
  );
}
