/**
 * Dictation rules shared by the web and native composers: recording limits,
 * the live waveform, checks before audio is sent, how failures are told
 * apart, and how a transcript joins the draft. Capturing audio is up to each
 * client; transcription with retries lives in `voiceTranscriptions.ts`.
 */

/** Keep recording limits aligned with backend/apps/harness/voice_schemas.py. */
export const VOICE_MAX_DURATION_MS = 10 * 60 * 1000;
export const VOICE_MAX_AUDIO_BYTES = 8 * 1024 * 1024;
export const VOICE_MAX_POINTERS = 12_000;
/** Shorter recordings are rejected before any audio leaves the device. */
export const VOICE_MIN_DURATION_MS = 200;
/** Both clients request this bit rate; ten minutes stay below the byte limit. */
export const VOICE_BITS_PER_SECOND = 64_000;
/** One transcription attempt, including cleanup, ends after this long. */
export const VOICE_TRANSCRIBE_TIMEOUT_MS = 300_000;
/** Number of recent 100 ms microphone levels kept for the live waveform. */
export const VOICE_LEVEL_HISTORY = 160;
/** How often clients sample the microphone level. */
export const VOICE_LEVEL_INTERVAL_MS = 100;

/** RMS below this is digital silence, not quiet speech. */
const SIGNAL_RMS = 0.00001;
/** Speech RMS rarely exceeds 0.1; scale it so the waveform uses its height. */
const LEVEL_GAIN = 12;

/** Limits for one recording; the server may only lower the shared maximum. */
export function voiceRecordingLimits(
  config?: {
    max_duration_ms?: number;
    max_audio_bytes?: number;
  } | null,
): { maxDurationMs: number; maxAudioBytes: number } {
  return {
    maxDurationMs: Math.min(
      config?.max_duration_ms || VOICE_MAX_DURATION_MS,
      VOICE_MAX_DURATION_MS,
    ),
    maxAudioBytes: Math.min(
      config?.max_audio_bytes || VOICE_MAX_AUDIO_BYTES,
      VOICE_MAX_AUDIO_BYTES,
    ),
  };
}

/**
 * A recording's length as the server takes it: whole milliseconds within the
 * limit. A recorder stopped at the limit reports a little more than it.
 */
export function voiceRecordingDuration(
  durationMs: number,
  maxDurationMs = VOICE_MAX_DURATION_MS,
): number {
  return Math.min(Math.max(1, Math.round(durationMs)), maxDurationMs);
}

/** Waveform level (0..1) of a microphone RMS amplitude (0..1). */
export function voiceLevelFromRms(rms: number): number {
  return Number.isFinite(rms) ? Math.min(1, Math.max(0, rms * LEVEL_GAIN)) : 0;
}

/** Waveform level (0..1) of a metering reading in dBFS, as native recorders report. */
export function voiceLevelFromDecibels(decibels: number): number {
  return Number.isFinite(decibels)
    ? voiceLevelFromRms(10 ** (Math.min(0, decibels) / 20))
    : 0;
}

/** Whether an RMS amplitude carries any signal; quiet speech does. */
export function voiceRmsHasSignal(rms: number): boolean {
  return Number.isFinite(rms) && rms >= SIGNAL_RMS;
}

/** Whether a dBFS metering reading carries any signal. */
export function voiceDecibelsHaveSignal(decibels: number): boolean {
  return Number.isFinite(decibels) && voiceRmsHasSignal(10 ** (decibels / 20));
}

/** The level history with `level` appended, oldest first, bounded. */
export function appendVoiceLevel(
  levels: readonly number[],
  level: number,
): readonly number[] {
  return [...levels.slice(1 - VOICE_LEVEL_HISTORY), level];
}

/**
 * Heights (0..1) of `count` waveform bars, newest first, so clients draw them
 * from the trailing edge. Missing history shows as quiet bars.
 */
export function voiceWaveformBars(
  levels: readonly number[],
  count: number,
): number[] {
  return Array.from({ length: Math.max(0, Math.floor(count)) }, (_, index) =>
    Math.min(1, Math.max(0, levels[levels.length - 1 - index] ?? 0)),
  );
}

/** `m:ss` of a recording's length. */
export function formatVoiceDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * Why a finished recording cannot be transcribed, or null when it can.
 * `hasSignal` is null when the client could not measure the microphone.
 */
export function voiceRecordingProblem(recording: {
  durationMs: number;
  bytes: number;
  hasSignal: boolean | null;
  maxAudioBytes?: number;
}): string | null {
  if (!recording.bytes || recording.durationMs < VOICE_MIN_DURATION_MS)
    return "The recording was too short. Please try again.";
  if (recording.bytes > (recording.maxAudioBytes ?? VOICE_MAX_AUDIO_BYTES))
    return "The recording exceeded the audio size limit. Please record a shorter message.";
  if (recording.hasSignal === false)
    return "No microphone audio was detected. Check your input device and try again.";
  return null;
}

/**
 * - `offline`: the server could not be reached; retried once there is a
 *   connection again, for as long as it takes.
 * - `unavailable`: the server or speech provider is busy or down; retried a
 *   few times after a pause.
 * - `timeout`: an attempt took too long; the user decides.
 * - `rejected`: the server refused the recording or its setup is missing.
 */
export type VoiceFailureKind =
  | "offline"
  | "unavailable"
  | "timeout"
  | "rejected";

export interface VoiceFailure {
  kind: VoiceFailureKind;
  message: string;
  /** Machine-readable server code, e.g. `voice_provider_missing`. */
  code?: string;
}

export const VOICE_OFFLINE_MESSAGE =
  "No connection. The recording is saved and transcribed once you’re back online.";
export const VOICE_TIMEOUT_MESSAGE =
  "Voice input processing timed out. Retry or discard the recording.";
const UNAVAILABLE_MESSAGE =
  "The speech service is unavailable right now. The recording is saved.";
const REJECTED_MESSAGE =
  "Voice input could not be processed. Retry or discard the recording.";
/** Gateway and rate-limit answers; the server is there but cannot help now. */
const TEMPORARY_STATUSES = new Set([429, 502, 503, 504]);

function field(error: unknown, name: string): unknown {
  return error && typeof error === "object"
    ? (error as Record<string, unknown>)[name]
    : undefined;
}

/** Whether `error` is an abort, which callers treat as cancellation. */
export function isVoiceAbort(error: unknown): boolean {
  return field(error, "name") === "AbortError";
}

/**
 * Classify a failed transcription request. Fetch reports an unreachable
 * server as a `TypeError` in browsers and React Native alike; the API client
 * reports its own deadline as status 0.
 */
export function voiceFailure(error: unknown): VoiceFailure {
  const status = field(error, "status");
  const code = field(error, "code");
  const detail = field(error, "message");
  const message = typeof detail === "string" && detail.trim() ? detail : "";
  const known = typeof code === "string" && code ? { code } : {};
  if (typeof status === "number") {
    if (status === 0)
      return { kind: "timeout", message: VOICE_TIMEOUT_MESSAGE, ...known };
    if (TEMPORARY_STATUSES.has(status))
      return {
        kind: "unavailable",
        message: message || UNAVAILABLE_MESSAGE,
        ...known,
      };
    return { kind: "rejected", message: message || REJECTED_MESSAGE, ...known };
  }
  if (field(error, "name") === "TypeError")
    return { kind: "offline", message: VOICE_OFFLINE_MESSAGE };
  return { kind: "rejected", message: message || REJECTED_MESSAGE };
}

/** Message for a microphone that could not be opened. */
export function voiceMicrophoneMessage(denied: boolean): string {
  return denied
    ? "Microphone access was denied. Allow it in Settings and try again."
    : "The microphone could not be opened. Check that it is available and try again.";
}

/**
 * What a recording beside the transcript shows: pointing at workspace images,
 * files or the desktop, or using an open app (the mobile app companion).
 */
export type DictationRecordingKind = "pointing" | "app";

const RECORDING_LABELS: Record<DictationRecordingKind, string> = {
  pointing:
    "Pointing recording (timeline of the transcript with timestamps, pointer, shown images and file lines, desktop frames)",
  app: "App recording (timeline of the transcript with timestamps, taps, screens and app logs)",
};

/**
 * The draft with a transcript appended on its own line, as both composers
 * insert dictation. A recording's timeline is referenced by its path.
 */
export function appendDictation(
  draft: string,
  text: string,
  recordingPath?: string,
  kind: DictationRecordingKind = "pointing",
): string {
  const addition = recordingPath
    ? `${text.trim()}\n\n${RECORDING_LABELS[kind]}: @file:${recordingPath}`
    : text.trim();
  return [draft.trimEnd(), addition].filter(Boolean).join("\n");
}
