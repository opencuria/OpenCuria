import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { AppState, Linking, Platform } from "react-native";
import { File } from "expo-file-system";
import { randomUUID } from "expo-crypto";
import {
  chatInputCacheKey,
  type ChatInputScope,
} from "@opencuria/chat-core/chatInputCache";
import {
  VOICE_LEVEL_INTERVAL_MS,
  appendDictation,
  appendVoiceLevel,
  formatVoiceDuration,
  voiceDecibelsHaveSignal,
  voiceLevelFromDecibels,
  voiceMicrophoneMessage,
  voiceRecordingLimits,
  voiceRecordingProblem,
} from "@opencuria/chat-core/voiceInput";
import {
  VoiceTranscriptionQueue,
  type VoiceClipEntry,
  type VoiceTranscriptText,
} from "@opencuria/chat-core/voiceTranscriptions";
import type { OpenCuriaClient } from "@opencuria/client";
import type {
  ComposerVoice,
  ComposerVoiceNotice,
  ComposerVoiceReceiver,
  ComposerVoiceRecording,
} from "../components/ComposerVoice";
import { lightTapHaptic } from "../ui/haptics";
import { AppVoiceConnectivity } from "./voiceConnectivity";
import { voiceClipFiles, type ChatVoiceClip } from "./voiceClipStore";
import {
  VOICE_MIME_TYPE,
  requestMicrophone,
  startVoiceCapture,
  type VoiceCapture,
} from "./voiceRecorder";

type DraftUpdate = (current: string) => string;

export interface VoiceDictationOptions {
  client: OpenCuriaClient | null;
  /** `appScope` of the signed-in account; null keeps dictation off. */
  account: string | null;
  /** The session cannot reach OpenCuria. */
  offline: boolean;
  /** The chat on screen; dictation needs its workspace. */
  scope: ChatInputScope | null;
  /** Change a chat's saved draft; resolves once it is on the device. */
  editDraft: (scope: ChatInputScope, update: DraftUpdate) => Promise<void>;
  /** Save drafts edited on screen now. */
  flushDrafts: () => Promise<void>;
  /** Open the settings page that sets up voice input. */
  onOpenSettings?: () => void;
}

/** A recording from the first tap until its file is in the queue. */
interface Session {
  scope: ChatInputScope;
  key: string;
  queue: VoiceTranscriptionQueue<ChatInputScope>;
  files: ReturnType<typeof voiceClipFiles>;
  capture: VoiceCapture | null;
  phase: ComposerVoiceRecording["phase"];
  cancelled: boolean;
  timer: ReturnType<typeof setInterval> | null;
  stops: Array<() => void>;
  elapsedMs: number;
  levels: readonly number[];
  /** Null until the platform measured the microphone. */
  hasSignal: boolean | null;
  /** Ticks the system has held the recording paused, e.g. for a call. */
  pausedTicks: number;
  maxDurationMs: number;
}

const NO_ENTRIES: readonly VoiceClipEntry<ChatInputScope>[] = [];
const noSubscription = () => () => undefined;
const noEntries = () => NO_ENTRIES;
/** A recording the system paused this long (a call) is finished and kept. */
const PAUSED_TICKS_LIMIT = 10;

/**
 * Dictation for the composer, as in the webapp. Recordings are saved on the
 * device before anything is sent and wait there through lost connections and
 * restarts; their text goes to the draft of the chat they were recorded in.
 * Leaving the chat or the app finishes a recording instead of losing it.
 */
export function useVoiceDictation(
  options: VoiceDictationOptions,
): ComposerVoice | undefined {
  const { account, offline, scope } = options;
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const scopeKey = scope ? chatInputCacheKey(scope) : null;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const scopeKeyRef = useRef(scopeKey);
  scopeKeyRef.current = scopeKey;
  const receiver = useRef<{ receive: ComposerVoiceReceiver } | null>(null);
  const [notice, setNotice] = useState<ComposerVoiceNotice | null>(null);
  const [recording, setRecording] = useState<
    (ComposerVoiceRecording & { key: string }) | null
  >(null);
  const session = useRef<Session | null>(null);

  const connectivity = useMemo(() => new AppVoiceConnectivity(), []);
  useEffect(() => () => connectivity.dispose(), [connectivity]);
  useEffect(() => {
    connectivity.setServerReachable(!offline);
  }, [connectivity, offline]);

  const files = useMemo(
    () => (account ? voiceClipFiles(account) : null),
    [account],
  );
  const queue = useMemo(() => {
    if (!files) return null;
    return new VoiceTranscriptionQueue<ChatInputScope>({
      store: files,
      connectivity,
      transcribe: async (clip, signal) => {
        const client = optionsRef.current.client;
        // Without a client the request cannot leave; wait like offline.
        if (!client) throw new TypeError("Not signed in.");
        let audio: string;
        try {
          audio = await new File(await files.audioUri(clip)).base64();
        } catch {
          throw new Error("The recording is no longer on this device.");
        }
        return client.voice.transcribe(
          clip.workspaceId,
          {
            audio_base64: audio,
            mime_type: clip.mimeType,
            duration_ms: clip.durationMs,
          },
          signal,
        );
      },
      deliver: (clip, transcript) => deliver(clip, transcript),
    });
    // `deliver` reads everything it needs from refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files, connectivity]);
  useEffect(() => () => queue?.dispose(), [queue]);
  const entries = useSyncExternalStore(
    queue?.subscribe ?? noSubscription,
    queue?.getSnapshot ?? noEntries,
    queue?.getSnapshot ?? noEntries,
  );
  const queueRef = useRef(queue);
  queueRef.current = queue;
  const filesRef = useRef(files);
  filesRef.current = files;

  /** The transcript joins its chat's draft; on screen the composer takes it. */
  async function deliver(
    clip: ChatVoiceClip,
    transcript: VoiceTranscriptText,
  ): Promise<void> {
    const text = transcript.text.trim();
    const onScreen = chatInputCacheKey(clip.target) === scopeKeyRef.current;
    if (!text) {
      if (onScreen)
        setNotice({
          tone: "neutral",
          message: "No speech was recognized in the recording.",
        });
      return;
    }
    const composer = onScreen ? receiver.current : null;
    if (composer) {
      composer.receive(text, Boolean(clip.send));
      await optionsRef.current.flushDrafts();
    } else {
      await optionsRef.current.editDraft(clip.target, (current) =>
        appendDictation(current, text),
      );
    }
    if (onScreen && transcript.cleanup_warning)
      setNotice({ tone: "neutral", message: transcript.cleanup_warning });
  }

  const publish = useCallback((current: Session | null) => {
    if (session.current !== current) return;
    setRecording(
      current
        ? {
            key: current.key,
            phase: current.phase,
            elapsedMs: current.elapsedMs,
            levels: current.levels,
          }
        : null,
    );
  }, []);

  const end = useCallback((current: Session) => {
    if (current.timer) clearInterval(current.timer);
    current.timer = null;
    for (const stop of current.stops.splice(0)) stop();
    if (session.current !== current) return;
    session.current = null;
    setRecording(null);
  }, []);

  const cancel = useCallback(() => {
    const current = session.current;
    if (!current || current.phase === "finishing") return;
    current.cancelled = true;
    end(current);
    lightTapHaptic();
    void current.capture?.cancel().catch(() => undefined);
  }, [end]);

  const finish = useCallback(
    async (send: boolean) => {
      const current = session.current;
      if (!current) return;
      if (current.phase === "starting") return cancel();
      if (current.phase !== "recording" || !current.capture) return;
      current.phase = "finishing";
      if (current.timer) clearInterval(current.timer);
      current.timer = null;
      publish(current);
      lightTapHaptic();
      try {
        const result = await current.capture.stop();
        const id = randomUUID();
        const bytes = await current.files.adopt(id, result.uri);
        const problem = voiceRecordingProblem({
          durationMs: result.durationMs,
          bytes,
          // Android reports silence for some working microphones; a silent
          // recording costs one request, a wrong rejection a dictation.
          hasSignal: Platform.OS === "ios" ? current.hasSignal : null,
        });
        if (problem) {
          await current.files.remove({ id } as ChatVoiceClip);
          setNotice({ tone: "error", message: problem });
          return;
        }
        await current.queue.add({
          id,
          workspaceId: current.scope.workspaceId,
          target: current.scope,
          mimeType: VOICE_MIME_TYPE,
          durationMs: Math.round(result.durationMs),
          bytes,
          createdAt: new Date().toISOString(),
          // Only sent while its chat is still on screen.
          send: send && current.key === scopeKeyRef.current,
        });
      } catch {
        setNotice({
          tone: "error",
          message: "The recording could not be saved. Please try again.",
        });
      } finally {
        end(current);
      }
    },
    [cancel, end, publish],
  );

  const tick = useCallback(
    (current: Session) => {
      if (!current.capture || current.phase !== "recording") return;
      const sample = current.capture.sample();
      current.elapsedMs = sample.durationMs;
      if (sample.decibels !== null)
        current.hasSignal =
          Boolean(current.hasSignal) ||
          voiceDecibelsHaveSignal(sample.decibels);
      current.levels = appendVoiceLevel(
        current.levels,
        sample.decibels === null ? 0 : voiceLevelFromDecibels(sample.decibels),
      );
      current.pausedTicks = sample.recording ? 0 : current.pausedTicks + 1;
      publish(current);
      if (current.elapsedMs >= current.maxDurationMs) {
        setNotice({
          tone: "neutral",
          message: `Recording stopped at the ${formatVoiceDuration(current.maxDurationMs)} limit.`,
        });
        void finish(false);
      } else if (current.pausedTicks >= PAUSED_TICKS_LIMIT) void finish(false);
    },
    [finish, publish],
  );

  /** Stop at once if voice input is not set up; offline, keep recording. */
  const checkSetup = useCallback(
    async (current: Session) => {
      const client = optionsRef.current.client;
      const config = await client?.voice
        .config(current.scope.workspaceId)
        .catch(() => null);
      if (!config || session.current !== current) return;
      current.maxDurationMs = voiceRecordingLimits(config).maxDurationMs;
      if (config.available) return;
      cancel();
      const settings = optionsRef.current.onOpenSettings;
      setNotice({
        tone: "error",
        message: config.reason
          ? `Voice input isn’t set up. ${config.reason}.`
          : "Voice input isn’t set up. Choose a speech model in Settings.",
        action: settings
          ? { label: "Open Settings", onPress: settings }
          : undefined,
      });
    },
    [cancel],
  );

  const start = useCallback(async () => {
    const target = scopeRef.current;
    const activeQueue = queueRef.current;
    const activeFiles = filesRef.current;
    if (!target || !activeQueue || !activeFiles || session.current) return;
    setNotice(null);
    const current: Session = {
      scope: target,
      key: chatInputCacheKey(target),
      queue: activeQueue,
      files: activeFiles,
      capture: null,
      phase: "starting",
      cancelled: false,
      timer: null,
      stops: [],
      elapsedMs: 0,
      levels: [],
      hasSignal: null,
      pausedTicks: 0,
      maxDurationMs: voiceRecordingLimits().maxDurationMs,
    };
    session.current = current;
    publish(current);
    lightTapHaptic();
    try {
      const access = await requestMicrophone();
      if (current.cancelled) return;
      if (access !== "granted") {
        end(current);
        setNotice({
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
        return;
      }
      void checkSetup(current);
      const capture = await startVoiceCapture();
      if (current.cancelled) {
        await capture.cancel();
        return;
      }
      current.capture = capture;
      current.phase = "recording";
      current.stops.push(capture.onInterrupted(() => void finish(false)));
      current.timer = setInterval(() => tick(current), VOICE_LEVEL_INTERVAL_MS);
      publish(current);
    } catch {
      if (current.cancelled) return;
      end(current);
      setNotice({ tone: "error", message: voiceMicrophoneMessage(false) });
    }
  }, [checkSetup, end, finish, publish, tick]);

  // Another chat or the background finishes a recording; nothing is lost.
  useEffect(() => {
    const current = session.current;
    if (current && current.key !== scopeKey) void finish(false);
    setNotice(null);
  }, [finish, scopeKey]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "background" && session.current) void finish(false);
    });
    return () => subscription.remove();
  }, [finish]);
  useEffect(
    () => () => {
      const current = session.current;
      if (!current) return;
      current.cancelled = true;
      end(current);
      void current.capture?.cancel().catch(() => undefined);
    },
    [end],
  );

  const receive = useCallback((receive: ComposerVoiceReceiver) => {
    const entry = { receive };
    receiver.current = entry;
    return () => {
      if (receiver.current === entry) receiver.current = null;
    };
  }, []);
  const retry = useCallback((id: string) => queueRef.current?.retry(id), []);
  const discard = useCallback(
    (id: string) => void queueRef.current?.discard(id),
    [],
  );
  const dismissNotice = useCallback(() => setNotice(null), []);
  const startRecording = useCallback(() => void start(), [start]);
  const finishRecording = useCallback(
    (send: boolean) => void finish(send),
    [finish],
  );

  const clips = useMemo(
    () =>
      entries
        .filter((entry) => chatInputCacheKey(entry.clip.target) === scopeKey)
        .map((entry) => ({
          id: entry.clip.id,
          durationMs: entry.clip.durationMs,
          status: entry.status,
        })),
    [entries, scopeKey],
  );
  const shown =
    recording && recording.key === scopeKey
      ? {
          phase: recording.phase,
          elapsedMs: recording.elapsedMs,
          levels: recording.levels,
        }
      : null;
  return useMemo(
    () =>
      scopeKey && queue
        ? {
            recording: shown,
            clips,
            notice,
            start: startRecording,
            finish: finishRecording,
            cancel,
            retry,
            discard,
            dismissNotice,
            receive,
          }
        : undefined,
    // `shown` is derived from `recording`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      queue,
      recording,
      scopeKey,
      clips,
      notice,
      startRecording,
      finishRecording,
      cancel,
      retry,
      discard,
      dismissNotice,
      receive,
    ],
  );
}
