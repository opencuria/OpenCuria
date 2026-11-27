import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  File,
  __directories,
  __files,
  __setUpload,
  __uploads,
} from "expo-file-system";
import { recordingFramePath } from "@opencuria/chat-core/visualRecording";
import type { OpenCuriaClient } from "@opencuria/client";
import { createLocalFiles } from "../platform/localFiles";
import { attachVisualClip } from "./visualClips";
import { VoiceClipFiles, type ChatVoiceClip } from "./voiceClipStore";

const RECORDING = "6a1d0f6e-6f8a-4c5e-9a0b-3d2f1e4c5b6a";
const TRANSCRIPT = {
  text: "Make this green.",
  segments: [{ start_ms: 100, end_ms: 900, text: "Make this green." }],
  timing: "provider" as const,
};

function files() {
  return new VoiceClipFiles(
    "https://curia.test/api/v1|org-a|7",
    createLocalFiles("opencuria-voice", "documents"),
  );
}

function clip(): ChatVoiceClip {
  return {
    id: "clip-1",
    workspaceId: "ws-1",
    target: { workspaceId: "ws-1", sessionId: "chat-1", harnessId: "native" },
    mimeType: "audio/mp4",
    durationMs: 2000,
    bytes: 5,
    createdAt: "2026-11-24T10:00:00.000Z",
    visual: { kind: "app", recordingId: RECORDING },
  };
}

function frame(name: string): string {
  const file = new File(`file:///cache/opencuria-app-recordings/${name}`);
  file.create({ intermediates: true });
  file.write("jpeg");
  return file.uri;
}

function client() {
  return {
    files: {
      createUpload: vi.fn(async () => ({
        uploadUrl: "https://curia.test/upload/1",
        expiresAt: 0,
        maxBytes: 1_000_000,
      })),
    },
    voice: {
      saveRecording: vi.fn(async () => ({
        path: `/workspace/.opencuria/recordings/${RECORDING}/timeline.md`,
      })),
    },
  } as unknown as OpenCuriaClient & {
    files: { createUpload: ReturnType<typeof vi.fn> };
    voice: { saveRecording: ReturnType<typeof vi.fn> };
  };
}

beforeEach(() => {
  __files.clear();
  __directories.clear();
  __uploads.length = 0;
  __setUpload(() => ({ status: 201, body: "{}", headers: {} }));
});

describe("attachVisualClip", () => {
  it("uploads the frames, saves the timeline and returns its path", async () => {
    const store = files();
    const first = recordingFramePath(RECORDING, 0);
    await store.adoptVisual(
      "clip-1",
      JSON.stringify({
        pointers: [
          {
            t_ms: 500,
            event: "down",
            x: 10,
            y: 20,
            mode: "point",
            target: { kind: "app", path: first, width: 540, height: 1200 },
          },
        ],
        frames: [{ t_ms: 0, path: first, texts: ["Todos"] }],
        events: [{ t_ms: 600, kind: "log", level: "info", message: "Added" }],
        app: { app_id: "app-1", name: "Todo Demo", platform: "android" },
      }),
      [frame("000000.jpg")],
    );
    const connection = client();
    const result = await attachVisualClip(
      connection,
      store,
      clip(),
      TRANSCRIPT,
      new AbortController().signal,
    );
    expect(connection.files.createUpload).toHaveBeenCalledWith(
      "ws-1",
      expect.objectContaining({
        directory: `/workspace/.opencuria/recordings/${RECORDING}/frames`,
        filename: "000000.jpg",
        conflict: "replace",
      }),
      expect.anything(),
    );
    expect(__uploads).toHaveLength(1);
    const manifest = connection.voice.saveRecording.mock.calls[0]![1];
    expect(manifest).toMatchObject({
      recording_id: RECORDING,
      duration_ms: 2000,
      text: "Make this green.",
      frames: [{ t_ms: 0, path: first, texts: ["Todos"] }],
      app: { name: "Todo Demo" },
    });
    expect(manifest.pointers).toHaveLength(1);
    expect(result.recording_path).toBe(
      `/workspace/.opencuria/recordings/${RECORDING}/timeline.md`,
    );
  });

  it("reports a refused upload with its status so the queue can retry or ask", async () => {
    const store = files();
    const first = recordingFramePath(RECORDING, 0);
    await store.adoptVisual(
      "clip-1",
      JSON.stringify({ pointers: [], frames: [{ t_ms: 0, path: first }] }),
      [frame("000000.jpg")],
    );
    __setUpload(() => ({
      status: 503,
      body: JSON.stringify({ detail: "Runner offline" }),
      headers: {},
    }));
    await expect(
      attachVisualClip(
        client(),
        store,
        clip(),
        TRANSCRIPT,
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ status: 503, message: "Runner offline" });
  });

  it("keeps the text without a timeline once the frames are gone", async () => {
    const connection = client();
    const result = await attachVisualClip(
      connection,
      files(),
      clip(),
      TRANSCRIPT,
      new AbortController().signal,
    );
    expect(result).toEqual(TRANSCRIPT);
    expect(connection.voice.saveRecording).not.toHaveBeenCalled();
  });
});
