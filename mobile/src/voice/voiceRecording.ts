import { useSyncExternalStore } from "react";
import { Linking, Platform } from "react-native";
import { randomUUID } from "expo-crypto";
import { File } from "expo-file-system";
import {
  chatInputCacheKey,
  type ChatInputScope,
} from "@opencuria/chat-core/chatInputCache";
import type { PointerMode } from "@opencuria/chat-core/visualRecording";
import { VisualRecorder } from "@opencuria/chat-core/visualRecorder";
import {
  VOICE_LEVEL_INTERVAL_MS,
  appendVoiceLevel,
  formatVoiceDuration,
  voiceDecibelsHaveSignal,
  voiceLevelFromDecibels,
  voiceMicrophoneMessage,
  voiceRecordingDuration,
  voiceRecordingLimits,
  voiceRecordingProblem,
} from "@opencuria/chat-core/voiceInput";
import type { OpenCuriaClient } from "@opencuria/client";
import type { ComposerVoiceNotice } from "../components/ComposerVoice";
import { lightTapHaptic } from "../ui/haptics";
import type { ChatVoiceClip } from "./voiceClipStore";
import type { SharedVoiceQueue } from "./voiceQueue";
import {
  VOICE_MIME_TYPE,
  requestMicrophone,
  startVoiceCapture,
  type VoiceCapture,
} from "./voiceRecorder";

/** A recording the system paused this long (a call) is finished and kept. */
const PAUSED_TICKS_LIMIT = 10;

/** `starting` waits for the microphone, `finishing` saves the files. */
export type RecordingPhase = "starting" | "recording" | "finishing";

/** What the floating recording bar and the composer show of a recording. */
export interface RecordingSnapshot {
  id: string;
  /** `chatInputCacheKey` of the chat whose draft gets the text. */
  key: string;
  phase: RecordingPhase;
  mode: PointerMode;
  /** The surface on top (an image, a file, an open app); null: the chat. */
  surface: { kind: "image" | "file" | "app"; pointable: boolean } | null;
}

/** Time and levels of the running recording, ten times a second. */
export interface RecordingMeter {
  elapsedMs: number;
  /** Recent microphone levels (0..1), oldest first. */
  levels: readonly number[];
}

const QUIET: RecordingMeter = { elapsedMs: 0, levels: [] };

/**
 * The meter outside React state: only what shows it re-renders ten times a
 * second, never the chat beneath (`useSyncExternalStore`).
 */
export class RecordingMeterStore {
  private value = QUIET;
  private readonly listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): RecordingMeter => this.value;

  set(value: RecordingMeter): void {
    this.value = value;
    for (const listener of [...this.listeners]) listener();
  }

  reset(): void {
    if (this.value !== QUIET) this.set(QUIET);
  }
}

export interface StartRecordingOptions {
  /** The React root that starts it; leaving its chat finishes it. */
  owner: symbol;
  scope: ChatInputScope;
  shared: Pick<SharedVoiceQueue, "files" | "queue">;
  client: OpenCuriaClient | null;
  /** Shows a problem where the recording was started (the composer). */
  notify: (notice: ComposerVoiceNotice) => void;
  /** Opens the settings page that sets up voice input. */
  onOpenSettings?: () => void;
  /** Keep an open app's sounds playing (the app companion). */
  mixWithOthers?: boolean;
}

function deleteFiles(uris: readonly string[]): void {
  for (const uri of uris)
    try {
      new File(uri).delete();
    } catch {
      // Already gone.
    }
}

/**
 * One recording from the first tap until its files are in the voice queue:
 * the microphone like dictation, and a `VisualRecorder` for what the user
 * shows meanwhile (images, files, an open app). There is one at a time, shared
 * by every React root (OpenCuria and the companion over an app), so the
 * composer, viewers and the app overlay all show and control the same one.
 */
export class VoiceRecording {
  readonly id = randomUUID();
  readonly key: string;
  readonly owner: symbol;
  readonly scope: ChatInputScope;
  readonly meter = new RecordingMeterStore();
  readonly visual: VisualRecorder;
  private snapshot: RecordingSnapshot;
  private capture: VoiceCapture | null = null;
  /** Epoch milliseconds of the audio's start; timeline times count from it. */
  private origin = Date.now();
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly cleanups: Array<() => unknown> = [];
  private stopInterrupted: (() => void) | null = null;
  private unsubscribeVisual: () => void;
  private elapsedMs = 0;
  private levels: readonly number[] = [];
  private hasSignal: boolean | null = null;
  private pausedTicks = 0;
  private maxDurationMs = voiceRecordingLimits().maxDurationMs;
  private ended = false;

  constructor(private readonly options: StartRecordingOptions) {
    this.owner = options.owner;
    this.scope = options.scope;
    this.key = chatInputCacheKey(options.scope);
    this.visual = new VisualRecorder({
      recordingId: this.id,
      now: () => Date.now() - this.origin,
    });
    this.snapshot = {
      id: this.id,
      key: this.key,
      phase: "starting",
      mode: "use",
      surface: null,
    };
    this.unsubscribeVisual = this.visual.subscribe(() => {
      const top = this.visual.top;
      this.update({
        surface: top ? { kind: top.kind, pointable: top.pointable } : null,
      });
    });
  }

  get state(): RecordingSnapshot {
    return this.snapshot;
  }

  get phase(): RecordingPhase {
    return this.snapshot.phase;
  }

  /** Epoch milliseconds the audio started at. */
  get startedAt(): number {
    return this.origin;
  }

  /** Recording time of an epoch time, e.g. of a native event. */
  time(epochMs: number): number {
    return epochMs - this.origin;
  }

  /** Run `cleanup` when the recording ends, e.g. to stop observing an app. */
  onEnd(cleanup: () => unknown): void {
    if (this.ended) void Promise.resolve().then(cleanup);
    else this.cleanups.push(cleanup);
  }

  setMode(mode: PointerMode): void {
    if (!this.ended) this.update({ mode });
  }

  /** Open the microphone; true once it records. */
  async start(): Promise<boolean> {
    const { notify } = this.options;
    lightTapHaptic();
    try {
      const access = await requestMicrophone();
      if (this.ended) return false;
      if (access !== "granted") {
        this.end();
        notify({
          tone: "error",
          message:
            access === "unavailable"
              ? "Dictation needs an updated version of this app."
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
      void this.checkSetup();
      const capture = await startVoiceCapture({
        mixWithOthers: this.options.mixWithOthers,
      });
      if (this.ended) {
        await capture.cancel();
        return false;
      }
      this.capture = capture;
      this.origin = Date.now();
      this.stopInterrupted = capture.onInterrupted(() => void this.finish());
      this.timer = setInterval(() => this.tick(), VOICE_LEVEL_INTERVAL_MS);
      this.update({ phase: "recording" });
      return true;
    } catch {
      if (this.ended) return false;
      this.end();
      notify({ tone: "error", message: voiceMicrophoneMessage(false) });
      return false;
    }
  }

  /**
   * Stop and queue the recording for its chat; with `send` its text is sent
   * once ready. What was shown becomes the visual timeline beside the audio.
   */
  async finish(send = false): Promise<void> {
    if (this.phase === "starting") return this.cancel();
    if (this.phase !== "recording" || !this.capture || this.ended) return;
    this.update({ phase: "finishing" });
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    lightTapHaptic();
    const { files, queue } = this.options.shared;
    let frames: string[] = [];
    try {
      const visual = await this.visual.finish();
      frames = visual.sources;
      await this.runCleanups();
      const result = await this.capture.stop();
      const id = randomUUID();
      const bytes = await files.adopt(id, result.uri);
      const problem = voiceRecordingProblem({
        durationMs: result.durationMs,
        bytes,
        // Android reports silence for some working microphones; a silent
        // recording costs one request, a wrong rejection a dictation.
        hasSignal: Platform.OS === "ios" ? this.hasSignal : null,
      });
      if (problem) {
        await files.remove({ id } as ChatVoiceClip);
        this.options.notify({ tone: "error", message: problem });
        return;
      }
      if (visual.result) {
        await files.adoptVisual(id, JSON.stringify(visual.result), frames);
        frames = [];
      }
      await queue.add({
        id,
        workspaceId: this.scope.workspaceId,
        target: this.scope,
        mimeType: VOICE_MIME_TYPE,
        // Stopped at the limit, the recorder reports a little more.
        durationMs: voiceRecordingDuration(
          result.durationMs,
          this.maxDurationMs,
        ),
        bytes,
        createdAt: new Date().toISOString(),
        send,
        ...(visual.result
          ? {
              visual: {
                kind: visual.result.app ? "app" : "pointing",
                recordingId: this.id,
              },
            }
          : {}),
      });
    } catch {
      this.options.notify({
        tone: "error",
        message: "The recording could not be saved. Please try again.",
      });
    } finally {
      deleteFiles(frames);
      this.end();
    }
  }

  /** Discard the recording and what it showed. */
  cancel(): void {
    if (this.ended || this.phase === "finishing") return;
    deleteFiles(this.visual.cancel());
    void this.capture?.cancel().catch(() => undefined);
    void this.runCleanups();
    lightTapHaptic();
    this.end();
  }

  private tick(): void {
    const capture = this.capture;
    if (!capture || this.phase !== "recording") return;
    const sample = capture.sample();
    this.elapsedMs = sample.durationMs;
    if (sample.decibels !== null)
      this.hasSignal =
        Boolean(this.hasSignal) || voiceDecibelsHaveSignal(sample.decibels);
    this.levels = appendVoiceLevel(
      this.levels,
      sample.decibels === null ? 0 : voiceLevelFromDecibels(sample.decibels),
    );
    this.pausedTicks = sample.recording ? 0 : this.pausedTicks + 1;
    this.visual.tick();
    this.meter.set({ elapsedMs: this.elapsedMs, levels: this.levels });
    if (this.elapsedMs >= this.maxDurationMs) {
      this.options.notify({
        tone: "neutral",
        message: `Recording stopped at the ${formatVoiceDuration(this.maxDurationMs)} limit.`,
      });
      void this.finish();
    } else if (this.pausedTicks >= PAUSED_TICKS_LIMIT) void this.finish();
  }

  /** Stop at once if voice input is not set up; offline, keep recording. */
  private async checkSetup(): Promise<void> {
    const config = await this.options.client?.voice
      .config(this.scope.workspaceId)
      .catch(() => null);
    if (!config || this.ended) return;
    this.maxDurationMs = voiceRecordingLimits(config).maxDurationMs;
    if (config.available) return;
    this.cancel();
    const settings = this.options.onOpenSettings;
    this.options.notify({
      tone: "error",
      message: config.reason
        ? `Voice input isn’t set up. ${config.reason}.`
        : "Voice input isn’t set up. Choose a speech model in Settings.",
      action: settings
        ? { label: "Open Settings", onPress: settings }
        : undefined,
    });
  }

  private async runCleanups(): Promise<void> {
    for (const cleanup of this.cleanups.splice(0))
      await Promise.resolve()
        .then(cleanup)
        .catch(() => undefined);
  }

  private update(change: Partial<RecordingSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...change };
    if (active === this) emit();
  }

  private end(): void {
    if (this.ended) return;
    this.ended = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.stopInterrupted?.();
    this.stopInterrupted = null;
    this.unsubscribeVisual();
    void this.runCleanups();
    this.meter.reset();
    if (active === this) {
      active = null;
      emit();
    }
  }
}

let active: VoiceRecording | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const getSnapshot = (): RecordingSnapshot | null => active?.state ?? null;

/** The running recording, whichever root started it. */
export function activeRecording(): VoiceRecording | null {
  return active;
}

/**
 * Start the one recording; resolves to it once the microphone records, or
 * null (another recording runs, or it could not start; `notify` says why).
 */
export async function startRecording(
  options: StartRecordingOptions,
): Promise<VoiceRecording | null> {
  if (active) return null;
  const recording = new VoiceRecording(options);
  active = recording;
  emit();
  return (await recording.start()) ? recording : null;
}

/** The running recording's state for React; null while none runs. */
export function useActiveRecording(): RecordingSnapshot | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** A recording runs in Point mode: fingers only show, surfaces stay put. */
export function usePointMode(): boolean {
  const recording = useActiveRecording();
  return recording?.phase === "recording" && recording.mode === "point";
}

/** Change Use/Point of the running recording. */
export function setRecordingMode(mode: PointerMode): void {
  active?.setMode(mode);
}

/** Stop the running recording; its text goes to its chat's draft. */
export function finishRecording(send = false): void {
  void active?.finish(send);
}

/** Discard the running recording. */
export function cancelRecording(): void {
  active?.cancel();
}
