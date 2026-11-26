import { CryptoDigestAlgorithm, digestStringAsync } from "expo-crypto";
import type { ChatInputScope } from "@opencuria/chat-core/chatInputCache";
import type {
  VoiceClip,
  VoiceClipStore,
} from "@opencuria/chat-core/voiceTranscriptions";
import {
  createLocalFiles,
  joinUri,
  type LocalFiles,
} from "../platform/localFiles";

/** A recording waiting for its text, which goes to a chat's draft. */
export type ChatVoiceClip = VoiceClip<ChatInputScope>;

const VERSION = 1;
const INDEX = "clips.json";
const AUDIO_EXTENSION = ".m4a";
/** Frames and the timeline of a recording with visuals, next to its audio. */
const VISUAL_EXTENSION = ".visual";
const VISUAL_TIMELINE = "timeline.json";

export type VoiceFiles = Pick<
  LocalFiles,
  | "root"
  | "exists"
  | "readText"
  | "writeText"
  | "makeDirectory"
  | "remove"
  | "move"
  | "size"
>;

type Hash = (value: string) => Promise<string>;

const sha256: Hash = (value) =>
  digestStringAsync(CryptoDigestAlgorithm.SHA256, value);

function optionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}

function isScope(value: unknown): value is ChatInputScope {
  const scope = value as Partial<ChatInputScope> | null;
  return Boolean(
    scope &&
    typeof scope.workspaceId === "string" &&
    optionalString(scope.sessionId) &&
    optionalString(scope.harnessId),
  );
}

function isClip(value: unknown): value is ChatVoiceClip {
  const clip = value as Partial<ChatVoiceClip> | null;
  return Boolean(
    clip &&
    typeof clip.id === "string" &&
    // The id names the audio file.
    /^[\w-]{1,64}$/.test(clip.id) &&
    typeof clip.workspaceId === "string" &&
    isScope(clip.target) &&
    typeof clip.mimeType === "string" &&
    typeof clip.durationMs === "number" &&
    typeof clip.bytes === "number" &&
    typeof clip.createdAt === "string" &&
    (clip.transcript === undefined ||
      typeof clip.transcript?.text === "string") &&
    (clip.visual === undefined ||
      ((clip.visual?.kind === "app" || clip.visual?.kind === "pointing") &&
        typeof clip.visual.recordingId === "string" &&
        /^[0-9a-f-]{36}$/i.test(clip.visual.recordingId))),
  );
}

/**
 * Recordings of one account (`appScope`) that wait for their text, kept in
 * the app's documents so the system never clears them like a cache. The
 * directory name is a hash, never an identifier; sign out deletes them all.
 */
export class VoiceClipFiles implements VoiceClipStore<ChatInputScope> {
  private directoryKey: Promise<string> | null = null;

  constructor(
    readonly scope: string,
    private readonly files: VoiceFiles,
    private readonly hash: Hash = sha256,
  ) {}

  /** `file://` URI of a clip's audio. */
  async audioUri(clip: Pick<ChatVoiceClip, "id">): Promise<string> {
    return joinUri(await this.directory(), `${clip.id}${AUDIO_EXTENSION}`);
  }

  /** Directory with the frames and timeline of a recording's clip. */
  async visualUri(clip: Pick<ChatVoiceClip, "id">): Promise<string> {
    return joinUri(await this.directory(), `${clip.id}${VISUAL_EXTENSION}`);
  }

  /**
   * Keep a recording's visual timeline (JSON) and frames for clip `id`; the
   * frames move in under their own names.
   */
  async adoptVisual(
    id: string,
    timeline: string,
    frames: readonly string[],
  ): Promise<void> {
    const visual = await this.visualUri({ id });
    this.files.makeDirectory(joinUri(visual, "frames"));
    for (const frame of frames)
      this.files.move(frame, this.visualFrameUri(visual, frame));
    this.files.writeText(joinUri(visual, VISUAL_TIMELINE), timeline);
  }

  /** The visual timeline of a clip; null when it is gone. */
  async visualTimeline(
    clip: Pick<ChatVoiceClip, "id">,
  ): Promise<string | null> {
    const uri = joinUri(await this.visualUri(clip), VISUAL_TIMELINE);
    return this.files.exists(uri) ? this.files.readText(uri) : null;
  }

  /** Where the frame named like `path` (a URI or workspace path) is kept. */
  visualFrameUri(visual: string, path: string): string {
    return joinUri(visual, "frames", path.slice(path.lastIndexOf("/") + 1));
  }

  /** Move a finished recording in as the audio of clip `id`; returns its size. */
  async adopt(id: string, recording: string): Promise<number> {
    this.files.makeDirectory(await this.directory());
    const target = await this.audioUri({ id });
    this.files.move(recording, target);
    return this.files.size(target);
  }

  async load(): Promise<ChatVoiceClip[]> {
    const uri = joinUri(await this.directory(), INDEX);
    if (!this.files.exists(uri)) return [];
    const stored = JSON.parse(await this.files.readText(uri)) as {
      version?: unknown;
      scope?: unknown;
      clips?: unknown;
    } | null;
    if (stored?.version !== VERSION || stored.scope !== this.scope) return [];
    const clips = Array.isArray(stored.clips)
      ? stored.clips.filter(isClip)
      : [];
    const kept: ChatVoiceClip[] = [];
    for (const clip of clips)
      // Without its audio, only a known transcript is worth keeping.
      if (clip.transcript || this.files.exists(await this.audioUri(clip)))
        kept.push(clip);
    return kept;
  }

  async save(clips: ChatVoiceClip[]): Promise<void> {
    const directory = await this.directory();
    this.files.makeDirectory(directory);
    this.files.writeText(
      joinUri(directory, INDEX),
      JSON.stringify({ version: VERSION, scope: this.scope, clips }),
    );
  }

  async remove(clip: ChatVoiceClip): Promise<void> {
    this.files.remove(await this.audioUri(clip));
    if (clip.visual) this.files.remove(await this.visualUri(clip));
  }

  private async directory(): Promise<string> {
    this.directoryKey ??= this.hash(`voice|${this.scope}`).then((digest) =>
      digest.slice(0, 32),
    );
    return joinUri(this.files.root, await this.directoryKey);
  }
}

let shared: VoiceFiles | null = null;

function voiceFiles(): VoiceFiles {
  shared ??= createLocalFiles("opencuria-voice", "documents");
  return shared;
}

/** The waiting recordings of one account on this device. */
export function voiceClipFiles(scope: string): VoiceClipFiles {
  return new VoiceClipFiles(scope, voiceFiles());
}

/** Delete every waiting recording, e.g. on sign out. */
export function clearVoiceClips(): void {
  try {
    const files = voiceFiles();
    files.remove(files.root);
  } catch {
    // Nothing saved, or storage unavailable.
  }
}
