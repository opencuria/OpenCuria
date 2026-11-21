import { beforeEach, describe, expect, it } from "vitest";
import { File, __directories, __files } from "expo-file-system";
import { createLocalFiles } from "../platform/localFiles";
import {
  VoiceClipFiles,
  clearVoiceClips,
  type ChatVoiceClip,
} from "./voiceClipStore";

const SCOPE = "https://curia.test/api/v1|org-a|7";

function store(scope = SCOPE) {
  return new VoiceClipFiles(
    scope,
    createLocalFiles("opencuria-voice", "documents"),
  );
}

function clip(id: string, extra: Partial<ChatVoiceClip> = {}): ChatVoiceClip {
  return {
    id,
    workspaceId: "ws-1",
    target: { workspaceId: "ws-1", sessionId: "chat-1", harnessId: null },
    mimeType: "audio/mp4",
    durationMs: 4200,
    bytes: 5,
    createdAt: "2026-11-20T10:00:00.000Z",
    ...extra,
  };
}

function recording(uri: string, content = "audio"): string {
  const file = new File(uri);
  file.create({ intermediates: true });
  file.write(content);
  return uri;
}

beforeEach(() => {
  __files.clear();
  __directories.clear();
});

describe("VoiceClipFiles", () => {
  it("keeps recordings in the app's documents under a hashed account directory", async () => {
    const clips = store();
    const size = await clips.adopt("a", recording("file:///cache/rec-1.m4a"));
    expect(size).toBe(5);
    const audio = await clips.audioUri({ id: "a" });
    expect(audio).toMatch(
      /^file:\/\/\/document\/opencuria-voice\/[0-9a-f]{32}\/a\.m4a$/,
    );
    expect(audio).not.toContain("org-a");
    expect(new File(audio).exists).toBe(true);
    expect(new File("file:///cache/rec-1.m4a").exists).toBe(false);

    await clips.save([clip("a")]);
    // A new store, as after a restart, finds the recording again.
    expect(await store().load()).toEqual([clip("a")]);
    // Another account sees nothing of it.
    expect(await store("https://curia.test/api/v1|org-b|7").load()).toEqual([]);
  });

  it("drops entries whose audio is gone unless their text is already known", async () => {
    const clips = store();
    await clips.adopt("kept", recording("file:///cache/rec-1.m4a"));
    await clips.save([
      clip("kept"),
      clip("lost"),
      clip("known", { transcript: { text: "Hello." } }),
      { ...clip("bad"), id: "../escape" },
    ]);
    expect((await clips.load()).map((entry) => entry.id)).toEqual([
      "kept",
      "known",
    ]);
  });

  it("deletes a recording's audio and every account's recordings on sign out", async () => {
    const clips = store();
    await clips.adopt("a", recording("file:///cache/rec-1.m4a"));
    await clips.adopt("b", recording("file:///cache/rec-2.m4a"));
    await clips.remove(clip("a"));
    expect(new File(await clips.audioUri({ id: "a" })).exists).toBe(false);
    expect(new File(await clips.audioUri({ id: "b" })).exists).toBe(true);
    clearVoiceClips();
    expect(new File(await clips.audioUri({ id: "b" })).exists).toBe(false);
  });
});
