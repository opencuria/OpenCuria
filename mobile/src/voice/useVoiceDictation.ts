import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { AppState } from "react-native";
import {
  chatInputCacheKey,
  type ChatInputScope,
} from "@opencuria/chat-core/chatInputCache";
import type { VoiceClipEntry } from "@opencuria/chat-core/voiceTranscriptions";
import type { OpenCuriaClient } from "@opencuria/client";
import type {
  ComposerVoice,
  ComposerVoiceNotice,
  ComposerVoiceReceiver,
} from "../components/ComposerVoice";
import {
  sharedVoiceQueue,
  type SharedVoiceQueue,
  type VoiceQueuePort,
} from "./voiceQueue";
import {
  activeRecording,
  startRecording,
  useActiveRecording,
} from "./voiceRecording";

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
  /** Keep an open app's sounds playing (the companion over an app). */
  mixWithOthers?: boolean;
}

/** Dictation for the composer, and recordings started elsewhere. */
export interface VoiceDictation extends ComposerVoice {
  /** Start recording for the chat on screen; true once it records. */
  record: () => Promise<boolean>;
}

const NO_ENTRIES: readonly VoiceClipEntry<ChatInputScope>[] = [];
const noSubscription = () => () => undefined;
const noEntries = () => NO_ENTRIES;
const NO_LEVELS: readonly number[] = [];

/**
 * Dictation for the composer, as in the webapp. Recordings are saved on the
 * device before anything is sent and wait there through lost connections and
 * restarts; their text goes to the draft of the chat they were recorded in.
 * While one runs, images, files and an open app the user shows join it
 * (`voiceRecording.ts`). Leaving the chat or the app finishes a recording
 * instead of losing it. Every root (OpenCuria, the app companion) shares one
 * queue per account and the one running recording.
 */
export function useVoiceDictation(
  options: VoiceDictationOptions,
): VoiceDictation | undefined {
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
  const [owner] = useState(() => Symbol("voice"));
  const recording = useActiveRecording();

  // One queue per account for every React root, so each recording is
  // transcribed and delivered once, to whichever root shows its chat.
  const port = useMemo<VoiceQueuePort>(
    () => ({
      client: () => optionsRef.current.client,
      shownKey: () => scopeKeyRef.current,
      receiver: () => receiver.current?.receive ?? null,
      editDraft: (target, update) =>
        optionsRef.current.editDraft(target, update),
      flushDrafts: () => optionsRef.current.flushDrafts(),
      notify: (next) => setNotice(next),
    }),
    [],
  );
  const [shared, setShared] = useState<SharedVoiceQueue | null>(null);
  useEffect(() => {
    if (!account) {
      setShared(null);
      return;
    }
    const next = sharedVoiceQueue(account);
    const release = next.acquire(port);
    setShared(next);
    return release;
  }, [account, port]);
  useEffect(() => {
    shared?.connectivity.setServerReachable(!offline);
  }, [shared, offline]);
  const queue = shared?.queue ?? null;
  const entries = useSyncExternalStore(
    queue?.subscribe ?? noSubscription,
    queue?.getSnapshot ?? noEntries,
    queue?.getSnapshot ?? noEntries,
  );
  const sharedRef = useRef(shared);
  sharedRef.current = shared;

  const record = useCallback(async () => {
    const target = scopeRef.current;
    const activeShared = sharedRef.current;
    if (!target || !activeShared) return false;
    setNotice(null);
    const started = await startRecording({
      owner,
      scope: target,
      shared: activeShared,
      client: optionsRef.current.client,
      notify: setNotice,
      onOpenSettings: optionsRef.current.onOpenSettings,
      mixWithOthers: optionsRef.current.mixWithOthers,
    });
    return Boolean(started);
  }, [owner]);

  /** The running recording if it belongs to the chat on screen. */
  const own = useCallback(() => {
    const current = activeRecording();
    return current && current.key === scopeKeyRef.current ? current : null;
  }, []);
  const cancel = useCallback(() => own()?.cancel(), [own]);
  const finish = useCallback(
    (send: boolean) =>
      // Only sent while its chat is still on screen.
      void own()?.finish(send),
    [own],
  );

  // Another chat or the background finishes a recording; nothing is lost.
  useEffect(() => {
    const current = activeRecording();
    if (current && current.owner === owner && current.key !== scopeKey)
      void current.finish(false);
    setNotice(null);
  }, [owner, scopeKey]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "background") void activeRecording()?.finish(false);
    });
    return () => subscription.remove();
  }, []);
  // A recording never outlives the root that started it.
  useEffect(
    () => () => {
      const current = activeRecording();
      if (current?.owner === owner) current.cancel();
    },
    [owner],
  );

  const receive = useCallback((receive: ComposerVoiceReceiver) => {
    const entry = { receive };
    receiver.current = entry;
    return () => {
      if (receiver.current === entry) receiver.current = null;
    };
  }, []);
  const queueRef = useRef(queue);
  queueRef.current = queue;
  const retry = useCallback((id: string) => queueRef.current?.retry(id), []);
  const discard = useCallback(
    (id: string) => void queueRef.current?.discard(id),
    [],
  );
  const dismissNotice = useCallback(() => setNotice(null), []);
  const startDictation = useCallback(() => void record(), [record]);

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
  const mine = recording && recording.key === scopeKey ? recording : null;
  const current = mine ? activeRecording() : null;
  // The bar reads time and levels from the meter itself, so the chat
  // around it does not render ten times a second.
  const shown = useMemo(
    () =>
      mine && current
        ? {
            phase: mine.phase,
            elapsedMs: 0,
            levels: NO_LEVELS,
            meter: current.meter,
          }
        : null,
    [current, mine],
  );
  return useMemo(
    () =>
      scopeKey && queue
        ? {
            recording: shown,
            clips,
            notice,
            start: startDictation,
            record,
            finish,
            cancel,
            retry,
            discard,
            dismissNotice,
            receive,
          }
        : undefined,
    [
      queue,
      shown,
      scopeKey,
      clips,
      notice,
      startDictation,
      record,
      finish,
      cancel,
      retry,
      discard,
      dismissNotice,
      receive,
    ],
  );
}
