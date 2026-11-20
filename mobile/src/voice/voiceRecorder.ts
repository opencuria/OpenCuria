import { Platform } from "react-native";
import { VOICE_BITS_PER_SECOND } from "@opencuria/chat-core/voiceInput";

type AudioModule = typeof import("expo-audio");
type KeepAwakeModule = typeof import("expo-keep-awake");
type Recorder = InstanceType<AudioModule["AudioModule"]["AudioRecorder"]>;

/** AAC in an MPEG-4 container; the server reads it as `audio/mp4`. */
export const VOICE_MIME_TYPE = "audio/mp4";
const KEEP_AWAKE_TAG = "opencuria-dictation";

let audio: AudioModule | null | undefined;
let keepAwake: KeepAwakeModule | null | undefined;

/**
 * Required on first use, as with haptics: a development client without the
 * native module reports dictation as unavailable instead of crashing.
 */
function audioModule(): AudioModule | null {
  if (audio === undefined) {
    try {
      audio = require("expo-audio") as AudioModule;
    } catch {
      audio = null;
    }
  }
  return audio;
}

function keepAwakeModule(): KeepAwakeModule | null {
  if (keepAwake === undefined) {
    try {
      keepAwake = require("expo-keep-awake") as KeepAwakeModule;
    } catch {
      keepAwake = null;
    }
  }
  return keepAwake;
}

export type MicrophoneAccess = "granted" | "denied" | "blocked" | "unavailable";

/** Ask for the microphone; `blocked` means only Settings can allow it now. */
export async function requestMicrophone(): Promise<MicrophoneAccess> {
  const module = audioModule();
  if (!module) return "unavailable";
  const current = await module.getRecordingPermissionsAsync();
  if (current.granted) return "granted";
  if (!current.canAskAgain) return "blocked";
  const requested = await module.requestRecordingPermissionsAsync();
  if (requested.granted) return "granted";
  return requested.canAskAgain ? "denied" : "blocked";
}

/** One reading of a running capture. */
export interface VoiceSample {
  durationMs: number;
  /** dBFS, or null when the platform did not measure. */
  decibels: number | null;
  /** False once the system paused the recording, e.g. for a call. */
  recording: boolean;
}

/** A running microphone recording into a file. */
export interface VoiceCapture {
  sample(): VoiceSample;
  /** Finish the file; resolves with where it is and how long it runs. */
  stop(): Promise<{ uri: string; durationMs: number }>;
  /** Stop and delete the file. */
  cancel(): Promise<void>;
  /** The system ended the recording (encoder error, media services reset). */
  onInterrupted(listener: () => void): () => void;
}

/** Mono speech at the bit rate the web client uses; ten minutes stay small. */
function recorderOptions(module: AudioModule): Record<string, unknown> {
  const common = {
    extension: ".m4a",
    sampleRate: 44_100,
    numberOfChannels: 1,
    bitRate: VOICE_BITS_PER_SECOND,
    isMeteringEnabled: true,
  };
  return Platform.OS === "ios"
    ? {
        ...common,
        outputFormat: module.IOSOutputFormat.MPEG4AAC,
        audioQuality: module.AudioQuality.HIGH,
      }
    : { ...common, outputFormat: "mpeg4", audioEncoder: "aac" };
}

async function releaseSession(module: AudioModule): Promise<void> {
  keepAwakeModule()?.deactivateKeepAwake(KEEP_AWAKE_TAG);
  // Playback (videos, published apps) gets the session back.
  await module
    .setAudioModeAsync({
      allowsRecording: false,
      interruptionMode: "mixWithOthers",
    })
    .catch(() => undefined);
}

/**
 * Start recording into the app's cache. Other audio pauses while the user
 * speaks and the screen stays on, so a long dictation is not cut by the lock.
 */
export async function startVoiceCapture(): Promise<VoiceCapture> {
  const module = audioModule();
  if (!module) throw new Error("Dictation needs an updated app.");
  await module.setAudioModeAsync({
    allowsRecording: true,
    playsInSilentMode: true,
    interruptionMode: "doNotMix",
  });
  let recorder: Recorder | null = null;
  try {
    recorder = new module.AudioModule.AudioRecorder(
      recorderOptions(module) as never,
    );
    await recorder.prepareToRecordAsync();
    recorder.record();
  } catch (error) {
    recorder?.release();
    await releaseSession(module);
    throw error;
  }
  void keepAwakeModule()
    ?.activateKeepAwakeAsync(KEEP_AWAKE_TAG)
    .catch(() => undefined);
  const active = recorder;
  const started = Date.now();
  let lastDuration = 0;
  let finished = false;
  const listeners = new Set<() => void>();
  const subscription = active.addListener("recordingStatusUpdate", (status) => {
    if (!finished && (status.hasError || status.mediaServicesDidReset))
      for (const listener of [...listeners]) listener();
  });
  const finish = async (): Promise<string> => {
    finished = true;
    subscription.remove();
    try {
      if (active.isRecording || active.getStatus().canRecord)
        await active.stop();
    } finally {
      await releaseSession(module);
    }
    const uri = active.uri ?? active.getStatus().url;
    active.release();
    if (!uri) throw new Error("The recording has no file.");
    return uri;
  };
  return {
    sample() {
      const status = active.getStatus();
      if (status.durationMillis > 0) lastDuration = status.durationMillis;
      return {
        durationMs: lastDuration,
        decibels:
          typeof status.metering === "number" &&
          Number.isFinite(status.metering)
            ? status.metering
            : null,
        recording: status.isRecording,
      };
    },
    async stop() {
      const status = active.getStatus();
      if (status.durationMillis > 0) lastDuration = status.durationMillis;
      const uri = await finish();
      // Paused time (a call) is not part of the recording.
      return { uri, durationMs: lastDuration || Date.now() - started };
    },
    async cancel() {
      const uri = await finish().catch(() => null);
      if (!uri) return;
      try {
        const { File } =
          require("expo-file-system") as typeof import("expo-file-system");
        const file = new File(uri);
        if (file.exists) file.delete();
      } catch {
        // The cache is cleared by the system eventually.
      }
    },
    onInterrupted(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
