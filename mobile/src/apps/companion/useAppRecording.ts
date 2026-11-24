import { useCallback, useEffect, useRef, useState } from "react";
import { Linking, Platform } from "react-native";
import { randomUUID } from "expo-crypto";
import { File } from "expo-file-system";
import type { ChatInputScope } from "@opencuria/chat-core/chatInputCache";
import {
  FrameCadence,
  PointerTimeline,
  VISUAL_MAX_APP_EVENTS,
  VISUAL_MAX_FRAMES,
  appRecordingEvent,
  frameAt,
  frameBounds,
  framePoint,
  frameTexts,
  pointerElement,
  recordingFramePath,
  type AppRecordingContext,
  type AppRecordingEvent,
  type PointerSample,
  type PointerTarget,
  type RecordingFrame,
} from "@opencuria/chat-core/visualRecording";
import {
  VOICE_LEVEL_INTERVAL_MS,
  appendVoiceLevel,
  formatVoiceDuration,
  voiceDecibelsHaveSignal,
  voiceLevelFromDecibels,
  voiceMicrophoneMessage,
  voiceRecordingLimits,
  voiceRecordingProblem,
} from "@opencuria/chat-core/voiceInput";
import type { OpenCuriaClient } from "@opencuria/client";
import { lightTapHaptic } from "../../ui/haptics";
import { sharedVoiceQueue } from "../../voice/voiceQueue";
import {
  VOICE_MIME_TYPE,
  requestMicrophone,
  startVoiceCapture,
  type VoiceCapture,
} from "../../voice/voiceRecorder";
import {
  captureAppFrame,
  onAppEvent,
  onAppTouch,
  recentAppEvents,
  startAppObservation,
  stopAppObservation,
  type AppEventRecord,
  type AppSurface,
  type AppTouchEvent,
} from "./companionNative";

/** Logs from this long before a recording come along: often the cause. */
export const EARLIER_EVENTS_MS = 60_000;
/** Audio louder than this counts as speech for the frame cadence. */
const SPEECH_LEVEL = 0.12;
/** A press samples its path this often while the finger moves. */
const MOVE_INTERVAL_MS = 100;
/** A recording the system paused this long (a call) is finished and kept. */
const PAUSED_TICKS_LIMIT = 10;

export type AppRecordingPhase = "idle" | "starting" | "recording" | "finishing";

/** How a finger meets the app while recording; see `PointerMode`. */
export type AppRecordingMode = "use" | "point";

export interface AppRecordingNotice {
  tone: "error" | "neutral";
  message: string;
  action?: { label: string; onPress: () => void };
}

export interface AppRecordingState {
  phase: AppRecordingPhase;
  mode: AppRecordingMode;
  notice: AppRecordingNotice | null;
}

/** Time and levels of the running recording, ten times a second. */
export interface AppRecordingMeter {
  elapsedMs: number;
  levels: readonly number[];
}

const QUIET: AppRecordingMeter = { elapsedMs: 0, levels: [] };

/**
 * The meter outside React state: only what shows it re-renders ten times a
 * second, never the companion's chat beneath (`useSyncExternalStore`).
 */
export class AppRecordingMeterStore {
  private value = QUIET;
  private readonly listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): AppRecordingMeter => this.value;

  set(value: AppRecordingMeter): void {
    this.value = value;
    for (const listener of [...this.listeners]) listener();
  }

  reset(): void {
    if (this.value !== QUIET) this.set(QUIET);
  }
}

export interface AppRecordingOptions {
  client: OpenCuriaClient | null;
  /** `appScope` of the signed-in account; recordings join its voice queue. */
  account: string | null;
  app: AppRecordingContext;
  /** Open Settings at voice input when it is not set up. */
  onOpenSettings?: () => void;
}

interface Session {
  id: string;
  scope: ChatInputScope;
  capture: VoiceCapture | null;
  surface: AppSurface | null;
  /** Epoch milliseconds of the audio's start; timeline times count from it. */
  origin: number;
  cancelled: boolean;
  finishing: boolean;
  timer: ReturnType<typeof setInterval> | null;
  subscriptions: Array<{ remove(): void }>;
  cadence: FrameCadence;
  pointers: PointerTimeline;
  frames: RecordingFrame[];
  frameSizes: Map<string, { width: number; height: number }>;
  frameUris: string[];
  previousFrameId: string | null;
  capturing: Promise<void> | null;
  events: AppRecordingEvent[];
  /** Touch events in arrival order; a press waits for its frame. */
  touches: Promise<void>;
  pressed: { lastMove: number } | null;
  elapsedMs: number;
  levels: readonly number[];
  hasSignal: boolean | null;
  pausedTicks: number;
  maxDurationMs: number;
}

const IDLE: AppRecordingState = {
  phase: "idle",
  mode: "use",
  notice: null,
};

/**
 * Talk while using an open app: the microphone records like dictation while
 * the app's screen, taps and pointing, logs and backend requests are kept on
 * one timeline. Finishing queues the recording like a dictation for the chat
 * it was started in; its text arrives in that chat's draft together with a
 * reference to the saved timeline (`appendDictation`, kind `app`).
 */
export function useAppRecording(options: AppRecordingOptions): {
  state: AppRecordingState;
  meter: AppRecordingMeterStore;
  start: (scope: ChatInputScope) => Promise<boolean>;
  finish: () => Promise<void>;
  cancel: () => void;
  setMode: (mode: AppRecordingMode) => void;
  dismissNotice: () => void;
} {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const session = useRef<Session | null>(null);
  const [state, setState] = useState<AppRecordingState>(IDLE);
  const [meter] = useState(() => new AppRecordingMeterStore());

  const publish = useCallback(
    (current: Session, phase: AppRecordingPhase) => {
      if (session.current !== current) return;
      setState((previous) =>
        previous.phase === phase ? previous : { ...previous, phase },
      );
      meter.set({ elapsedMs: current.elapsedMs, levels: current.levels });
    },
    [meter],
  );

  const notify = useCallback((notice: AppRecordingNotice | null) => {
    setState((previous) => ({ ...previous, notice }));
  }, []);

  /** The frame on screen now: captured, or the previous one if unchanged. */
  const captureScreen = useCallback((current: Session): Promise<void> => {
    if (current.capturing) return current.capturing;
    const surface = current.surface;
    if (!surface || current.frames.length >= VISUAL_MAX_FRAMES)
      return Promise.resolve();
    const index = current.frames.length;
    current.cadence.captured(Date.now());
    const run = captureAppFrame(current.id, index, current.previousFrameId)
      .then((frame) => {
        if (current.cancelled || frame.unchanged) return;
        const t = Math.max(0, frame.capturedAt - current.origin);
        const path = recordingFramePath(current.id, index);
        current.frames.push({ t_ms: t, path, texts: frameTexts(frame.texts) });
        current.frameSizes.set(path, {
          width: frame.width,
          height: frame.height,
        });
        current.frameUris.push(frame.uri);
        current.previousFrameId = frame.frameId;
      })
      .catch(() => {
        // A frame that failed only leaves a gap in the timeline.
      })
      .finally(() => {
        current.capturing = null;
      });
    current.capturing = run;
    return run;
  }, []);

  const targetAt = (
    current: Session,
    t: number,
  ): {
    frame: RecordingFrame;
    size: { width: number; height: number };
  } | null => {
    const frame = frameAt(current.frames, t);
    const size = frame && current.frameSizes.get(frame.path);
    return frame && size ? { frame, size } : null;
  };

  const addTouch = useCallback(
    async (current: Session, event: AppTouchEvent) => {
      if (current.cancelled || !current.surface) return;
      const now = Date.now();
      current.cadence.activity(now);
      if (event.phase === "down") {
        current.pressed = { lastMove: event.t };
        // The press refers to what was on screen: capture it now.
        await captureScreen(current);
      } else if (event.phase === "move") {
        if (!current.pressed) return;
        if (event.t - current.pressed.lastMove < MOVE_INTERVAL_MS) return;
        current.pressed.lastMove = event.t;
      } else {
        current.pressed = null;
      }
      const t = Math.max(0, event.t - current.origin);
      // A press uses the frame just captured; a move or release the one shown.
      const shown =
        targetAt(
          current,
          event.phase === "down" ? Date.now() - current.origin : t,
        ) ?? targetAt(current, Number.MAX_SAFE_INTEGER);
      const point = shown && framePoint(event, current.surface, shown.size);
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
        event.phase === "down" && event.element
          ? pointerElement({
              ...event.element,
              bounds:
                event.element.bounds && shown
                  ? frameBounds(
                      event.element.bounds,
                      current.surface,
                      shown.size,
                    )
                  : undefined,
            })
          : undefined;
      const sample: PointerSample = {
        t_ms: t,
        event:
          event.phase === "cancel"
            ? "up"
            : (event.phase as PointerSample["event"]),
        x: target ? point!.x : null,
        y: target ? point!.y : null,
        target,
        mode: event.mode,
        ...(element ? { element } : {}),
      };
      current.pointers.add(sample);
    },
    [captureScreen],
  );

  const addEvent = (current: Session, record: AppEventRecord) => {
    if (current.events.length >= VISUAL_MAX_APP_EVENTS) return;
    const before = current.origin - record.t;
    const event = appRecordingEvent({
      t_ms: Math.max(0, record.t - current.origin),
      kind: record.kind,
      level: record.level,
      // Logs from before the recording keep their age.
      message:
        before > 0 && record.message
          ? `(${(before / 1000).toFixed(1)} s before recording) ${record.message}`
          : record.message,
      method: record.method,
      url: record.url,
      status: record.status,
      duration_ms: record.durationMs,
    });
    if (event) current.events.push(event);
  };

  const end = useCallback((current: Session) => {
    if (current.timer) clearInterval(current.timer);
    current.timer = null;
    for (const subscription of current.subscriptions.splice(0))
      subscription.remove();
    if (session.current === current) session.current = null;
  }, []);

  const discardFrames = (current: Session) => {
    for (const uri of current.frameUris)
      try {
        new File(uri).delete();
      } catch {
        // Already gone.
      }
  };

  const cancel = useCallback(() => {
    const current = session.current;
    if (!current) return;
    current.cancelled = true;
    end(current);
    lightTapHaptic();
    void current.capture?.cancel().catch(() => undefined);
    void stopAppObservation().catch(() => undefined);
    discardFrames(current);
    meter.reset();
    setState((previous) => ({ ...IDLE, notice: previous.notice }));
  }, [end, meter]);

  const finish = useCallback(async () => {
    const current = session.current;
    if (!current || current.cancelled || current.finishing) return;
    if (!current.capture) return cancel();
    current.finishing = true;
    publish(current, "finishing");
    if (current.timer) clearInterval(current.timer);
    current.timer = null;
    lightTapHaptic();
    const { account, app } = optionsRef.current;
    try {
      // The last screen, then the pending touches.
      current.cadence.reset();
      await captureScreen(current);
      await current.touches;
      for (const subscription of current.subscriptions.splice(0))
        subscription.remove();
      await stopAppObservation().catch(() => undefined);
      const result = await current.capture.stop();
      if (!account) throw new Error("Not signed in.");
      const shared = sharedVoiceQueue(account);
      const id = randomUUID();
      const bytes = await shared.files.adopt(id, result.uri);
      const problem = voiceRecordingProblem({
        durationMs: result.durationMs,
        bytes,
        // Android reports silence for some working microphones.
        hasSignal: Platform.OS === "ios" ? current.hasSignal : null,
      });
      if (problem) {
        await shared.files.remove({ id } as never);
        discardFrames(current);
        notify({ tone: "error", message: problem });
        return;
      }
      await shared.files.adoptVisual(
        id,
        JSON.stringify({
          pointers: current.pointers.result(),
          frames: current.frames,
          events: current.events,
          app,
        }),
        current.frameUris,
      );
      await shared.queue.add({
        id,
        workspaceId: current.scope.workspaceId,
        target: current.scope,
        mimeType: VOICE_MIME_TYPE,
        durationMs: Math.round(result.durationMs),
        bytes,
        createdAt: new Date().toISOString(),
        visual: { kind: "app", recordingId: current.id },
      });
    } catch {
      discardFrames(current);
      notify({
        tone: "error",
        message: "The recording could not be saved. Please try again.",
      });
    } finally {
      end(current);
      meter.reset();
      setState((previous) => ({ ...IDLE, notice: previous.notice }));
    }
  }, [cancel, captureScreen, end, meter, notify, publish]);

  const tick = useCallback(
    (current: Session) => {
      if (!current.capture || current.cancelled) return;
      const sample = current.capture.sample();
      const now = Date.now();
      current.elapsedMs = sample.durationMs;
      const level =
        sample.decibels === null ? 0 : voiceLevelFromDecibels(sample.decibels);
      if (sample.decibels !== null)
        current.hasSignal =
          Boolean(current.hasSignal) ||
          voiceDecibelsHaveSignal(sample.decibels);
      current.levels = appendVoiceLevel(current.levels, level);
      // Speaking is activity: the screen being talked about is captured.
      if (level >= SPEECH_LEVEL) current.cadence.activity(now);
      if (current.cadence.due(now)) void captureScreen(current);
      current.pausedTicks = sample.recording ? 0 : current.pausedTicks + 1;
      publish(current, "recording");
      if (current.elapsedMs >= current.maxDurationMs) {
        notify({
          tone: "neutral",
          message: `Recording stopped at the ${formatVoiceDuration(current.maxDurationMs)} limit.`,
        });
        void finish();
      } else if (current.pausedTicks >= PAUSED_TICKS_LIMIT) void finish();
    },
    [captureScreen, finish, notify, publish],
  );

  const start = useCallback(
    async (scope: ChatInputScope): Promise<boolean> => {
      if (session.current) return false;
      const { client, account } = optionsRef.current;
      if (!client || !account) {
        notify({
          tone: "error",
          message: "Sign in to OpenCuria to talk about this app.",
        });
        return false;
      }
      notify(null);
      const current: Session = {
        id: randomUUID(),
        scope,
        capture: null,
        surface: null,
        origin: Date.now(),
        cancelled: false,
        finishing: false,
        timer: null,
        subscriptions: [],
        cadence: new FrameCadence(Date.now()),
        pointers: new PointerTimeline(),
        frames: [],
        frameSizes: new Map(),
        frameUris: [],
        previousFrameId: null,
        capturing: null,
        events: [],
        touches: Promise.resolve(),
        pressed: null,
        elapsedMs: 0,
        levels: [],
        hasSignal: null,
        pausedTicks: 0,
        maxDurationMs: voiceRecordingLimits().maxDurationMs,
      };
      session.current = current;
      setState({ ...IDLE, phase: "starting" });
      lightTapHaptic();
      try {
        const config = await client.voice
          .config(scope.workspaceId)
          .catch(() => null);
        if (current.cancelled) return false;
        if (config && !config.available) {
          end(current);
          setState(IDLE);
          const settings = optionsRef.current.onOpenSettings;
          notify({
            tone: "error",
            message: config.reason
              ? `Voice input isn’t set up. ${config.reason}.`
              : "Voice input isn’t set up. Choose a speech model in Settings.",
            action: settings
              ? { label: "Open Settings", onPress: settings }
              : undefined,
          });
          return false;
        }
        if (config)
          current.maxDurationMs = voiceRecordingLimits(config).maxDurationMs;
        const access = await requestMicrophone();
        if (current.cancelled) return false;
        if (access !== "granted") {
          end(current);
          setState(IDLE);
          notify({
            tone: "error",
            message:
              access === "unavailable"
                ? "Recording needs an updated version of OpenCuria."
                : voiceMicrophoneMessage(true),
            action:
              access === "blocked"
                ? {
                    label: "Open Settings",
                    onPress: () => void Linking.openSettings(),
                  }
                : undefined,
          });
          return false;
        }
        current.surface = await startAppObservation(current.id);
        const audio = await startVoiceCapture({ mixWithOthers: true });
        if (current.cancelled) {
          await audio.cancel();
          await stopAppObservation().catch(() => undefined);
          return false;
        }
        current.capture = audio;
        current.origin = Date.now();
        current.cadence = new FrameCadence(current.origin);
        current.subscriptions.push(
          { remove: audio.onInterrupted(() => void finish()) },
          onAppTouch((event) => {
            current.touches = current.touches.then(() =>
              addTouch(current, event).catch(() => undefined),
            );
          }),
          onAppEvent((event) => addEvent(current, event)),
        );
        for (const event of await recentAppEvents(
          current.origin - EARLIER_EVENTS_MS,
        ))
          addEvent(current, event);
        // The screen as the recording starts.
        void captureScreen(current);
        current.timer = setInterval(
          () => tick(current),
          VOICE_LEVEL_INTERVAL_MS,
        );
        publish(current, "recording");
        return true;
      } catch {
        if (current.cancelled) return false;
        end(current);
        void current.capture?.cancel().catch(() => undefined);
        void stopAppObservation().catch(() => undefined);
        setState(IDLE);
        notify({ tone: "error", message: voiceMicrophoneMessage(false) });
        return false;
      }
    },
    [addTouch, captureScreen, end, finish, notify, publish, tick],
  );

  const setMode = useCallback((mode: AppRecordingMode) => {
    setState((previous) => ({ ...previous, mode }));
  }, []);

  const dismissNotice = useCallback(() => notify(null), [notify]);

  // A recording never outlives the companion.
  useEffect(
    () => () => {
      const current = session.current;
      if (!current) return;
      current.cancelled = true;
      end(current);
      void current.capture?.cancel().catch(() => undefined);
      void stopAppObservation().catch(() => undefined);
    },
    [end],
  );

  return { state, meter, start, finish, cancel, setMode, dismissNotice };
}
