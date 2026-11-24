import { beforeEach, describe, expect, it, vi } from "vitest";
import { File, __directories, __files } from "expo-file-system";
import { chatInputCacheKey } from "@opencuria/chat-core/chatInputCache";
import type { OpenCuriaClient } from "@opencuria/client";
import type { ChatVoiceClip } from "./voiceClipStore";
import { sharedVoiceQueue, type VoiceQueuePort } from "./voiceQueue";

vi.mock("./voiceConnectivity", () => ({
  AppVoiceConnectivity: class {
    online = () => true;
    watch = () => () => undefined;
    setServerReachable = () => undefined;
    dispose = () => undefined;
  },
}));
const attach = vi.hoisted(() => vi.fn());
vi.mock("./visualClips", () => ({ attachVisualClip: attach }));

const ACCOUNT = "https://curia.test/api/v1|org-a|7";
const CHAT = { workspaceId: "ws-1", sessionId: "chat-1", harnessId: "native" };
const OTHER = { workspaceId: "ws-1", sessionId: "chat-2", harnessId: "native" };

function client(text = "Make it green.") {
  return {
    voice: {
      transcribe: vi.fn(async () => ({
        text,
        segments: [{ start_ms: 0, end_ms: 900, text }],
        timing: "provider",
      })),
    },
  } as unknown as OpenCuriaClient;
}

function port(
  shown: typeof CHAT | null,
  connection: OpenCuriaClient | null,
  receive?: VoiceQueuePort["receiver"] extends () => infer R ? R : never,
): VoiceQueuePort & {
  editDraft: ReturnType<typeof vi.fn>;
  notify: ReturnType<typeof vi.fn>;
} {
  return {
    client: () => connection,
    shownKey: () => (shown ? chatInputCacheKey(shown) : null),
    receiver: () => receive ?? null,
    editDraft: vi.fn(async () => undefined),
    flushDrafts: async () => undefined,
    notify: vi.fn(),
  };
}

async function clip(
  id: string,
  extra: Partial<ChatVoiceClip> = {},
): Promise<ChatVoiceClip> {
  const shared = sharedVoiceQueue(ACCOUNT);
  const audio = new File(`file:///cache/${id}.m4a`);
  audio.create({ intermediates: true });
  audio.write("audio");
  const bytes = await shared.files.adopt(id, audio.uri);
  return {
    id,
    workspaceId: "ws-1",
    target: CHAT,
    mimeType: "audio/mp4",
    durationMs: 1500,
    bytes,
    createdAt: new Date().toISOString(),
    ...extra,
  };
}

async function settle(): Promise<void> {
  for (let index = 0; index < 50; index++) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  for (let index = 0; index < 50; index++) await Promise.resolve();
}

beforeEach(() => {
  __files.clear();
  __directories.clear();
  attach.mockReset();
});

describe("sharedVoiceQueue", () => {
  it("transcribes once for every root and hands the text to the one showing its chat", async () => {
    const connection = client();
    const receive = vi.fn();
    const main = port(OTHER, connection);
    const companion = port(CHAT, null, receive);
    const shared = sharedVoiceQueue(ACCOUNT);
    expect(sharedVoiceQueue(ACCOUNT)).toBe(shared);
    const releaseMain = shared.acquire(main);
    const releaseCompanion = shared.acquire(companion);
    await shared.queue.add(await clip("clip-1"));
    await settle();
    expect(connection.voice.transcribe).toHaveBeenCalledTimes(1);
    expect(receive).toHaveBeenCalledWith("Make it green.", false, undefined);
    expect(main.editDraft).not.toHaveBeenCalled();
    releaseCompanion();
    releaseMain();
    // The last release ends the queue; the next user gets a new one.
    expect(sharedVoiceQueue(ACCOUNT)).not.toBe(shared);
  });

  it("references an app recording's timeline in the draft of its chat", async () => {
    attach.mockImplementation(async (_client, _files, _clip, transcript) => ({
      ...transcript,
      recording_path: "/workspace/.opencuria/recordings/r/timeline.md",
    }));
    const shared = sharedVoiceQueue(ACCOUNT);
    // No composer shows the chat: the text goes into its saved draft.
    const main = port(OTHER, client("Tap here."));
    const release = shared.acquire(main);
    await shared.queue.add(
      await clip("clip-2", {
        visual: {
          kind: "app",
          recordingId: "6a1d0f6e-6f8a-4c5e-9a0b-3d2f1e4c5b6a",
        },
      }),
    );
    await settle();
    expect(attach).toHaveBeenCalledTimes(1);
    expect(main.editDraft).toHaveBeenCalledWith(CHAT, expect.any(Function));
    const update = main.editDraft.mock.calls[0]![1] as (
      draft: string,
    ) => string;
    expect(update("Draft")).toBe(
      "Draft\nTap here.\n\nApp recording (timeline of the transcript with timestamps, taps, screens and app logs): @file:/workspace/.opencuria/recordings/r/timeline.md",
    );
    release();
  });

  it("tells the chat on screen when no speech was recognized", async () => {
    const shared = sharedVoiceQueue(ACCOUNT);
    const companion = port(CHAT, client(" "), vi.fn());
    const release = shared.acquire(companion);
    await shared.queue.add(await clip("clip-3"));
    await settle();
    expect(companion.notify).toHaveBeenCalledWith({
      tone: "neutral",
      message: "No speech was recognized in the recording.",
    });
    expect(companion.editDraft).not.toHaveBeenCalled();
    release();
  });
});
