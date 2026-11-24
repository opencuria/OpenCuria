import { File } from "expo-file-system";
import {
  chatInputCacheKey,
  type ChatInputScope,
} from "@opencuria/chat-core/chatInputCache";
import {
  appendDictation,
  type DictationRecordingKind,
} from "@opencuria/chat-core/voiceInput";
import {
  VoiceTranscriptionQueue,
  type VoiceTranscriptText,
} from "@opencuria/chat-core/voiceTranscriptions";
import type { OpenCuriaClient } from "@opencuria/client";
import type {
  ComposerVoiceNotice,
  ComposerVoiceReceiver,
} from "../components/ComposerVoice";
import { attachVisualClip } from "./visualClips";
import { AppVoiceConnectivity } from "./voiceConnectivity";
import {
  voiceClipFiles,
  type ChatVoiceClip,
  type VoiceClipFiles,
} from "./voiceClipStore";

type DraftUpdate = (current: string) => string;

/**
 * One React root that dictates into chats: OpenCuria's main screen or the
 * companion over an open app. Both may show the same chat.
 */
export interface VoiceQueuePort {
  client(): OpenCuriaClient | null;
  /** `chatInputCacheKey` of the chat on screen in this root. */
  shownKey(): string | null;
  /** The composer on screen, which inserts text where the user expects it. */
  receiver(): ComposerVoiceReceiver | null;
  /** Change any chat's saved draft (the account's shared draft store). */
  editDraft(scope: ChatInputScope, update: DraftUpdate): Promise<void>;
  flushDrafts(): Promise<void>;
  notify(notice: ComposerVoiceNotice): void;
}

/**
 * The waiting recordings of one account and their transcription queue, shared
 * by every root so a recording is transcribed and delivered exactly once.
 */
export class SharedVoiceQueue {
  readonly files: VoiceClipFiles;
  readonly connectivity = new AppVoiceConnectivity();
  readonly queue: VoiceTranscriptionQueue<ChatInputScope>;
  private readonly ports = new Set<VoiceQueuePort>();
  private users = 0;

  constructor(readonly account: string) {
    this.files = voiceClipFiles(account);
    this.queue = new VoiceTranscriptionQueue<ChatInputScope>({
      store: this.files,
      connectivity: this.connectivity,
      transcribe: async (clip, signal) => {
        const client = this.client();
        // Without a client the request cannot leave; wait like offline.
        if (!client) throw new TypeError("Not signed in.");
        let audio: string;
        try {
          audio = await new File(await this.files.audioUri(clip)).base64();
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
      attach: async (clip, transcript, signal) => {
        if (!clip.visual) return transcript;
        const client = this.client();
        if (!client) throw new TypeError("Not signed in.");
        return attachVisualClip(client, this.files, clip, transcript, signal);
      },
      deliver: (clip, transcript) => this.deliver(clip, transcript),
    });
  }

  /** Start using the queue; `release` when the root goes away. */
  acquire(port: VoiceQueuePort): () => void {
    this.ports.add(port);
    this.users += 1;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.ports.delete(port);
      this.users -= 1;
      if (this.users > 0) return;
      queues.delete(this.account);
      this.queue.dispose();
      this.connectivity.dispose();
    };
  }

  private client(): OpenCuriaClient | null {
    for (const port of this.ports) {
      const client = port.client();
      if (client) return client;
    }
    return null;
  }

  /** The transcript joins its chat's draft; a composer showing it takes it. */
  private async deliver(
    clip: ChatVoiceClip,
    transcript: VoiceTranscriptText,
  ): Promise<void> {
    const key = chatInputCacheKey(clip.target);
    const showing = [...this.ports].filter((port) => port.shownKey() === key);
    const text = transcript.text.trim();
    if (!text) {
      for (const port of showing)
        port.notify({
          tone: "neutral",
          message: "No speech was recognized in the recording.",
        });
      return;
    }
    const recording = transcript.recording_path
      ? {
          path: transcript.recording_path,
          kind: (clip.visual?.kind ?? "pointing") as DictationRecordingKind,
        }
      : undefined;
    const composer = showing.find((port) => port.receiver());
    if (composer) {
      composer.receiver()!(text, Boolean(clip.send), recording);
      await composer.flushDrafts();
    } else {
      const port = showing[0] ?? [...this.ports][0];
      if (!port) throw new Error("No chat can take the text right now.");
      await port.editDraft(clip.target, (current) =>
        appendDictation(current, text, recording?.path, recording?.kind),
      );
    }
    if (transcript.cleanup_warning)
      for (const port of showing)
        port.notify({ tone: "neutral", message: transcript.cleanup_warning });
  }
}

const queues = new Map<string, SharedVoiceQueue>();

/** The shared queue of an account (`appScope`); `acquire` it to use it. */
export function sharedVoiceQueue(account: string): SharedVoiceQueue {
  let shared = queues.get(account);
  if (!shared) {
    shared = new SharedVoiceQueue(account);
    queues.set(account, shared);
  }
  return shared;
}
