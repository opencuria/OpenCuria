import { act, renderHook } from "@testing-library/react-native";
import type { VisualSurfaceSpec } from "@opencuria/chat-core/visualRecorder";
import type { OpenCuriaClient } from "@opencuria/client";
import { useRecordingSurface } from "./useRecordingSurface";
import {
  activeRecording,
  cancelRecording,
  finishRecording,
  setRecordingMode,
  startRecording,
  useActiveRecording,
  usePointMode,
} from "./voiceRecording";

const RECORDING = "6a1d0f6e-6f8a-4c5e-9a0b-3d2f1e4c5b6a";
const SCOPE = { workspaceId: "ws-1", sessionId: "chat-1", harnessId: "native" };

const mockCapture = {
  sample: jest.fn(() => ({ durationMs: 1200, decibels: -30, recording: true })),
  stop: jest.fn(async () => ({
    uri: "file:///cache/rec.m4a",
    durationMs: 4200,
  })),
  cancel: jest.fn(async () => undefined),
  onInterrupted: jest.fn(() => () => undefined),
};
jest.mock("./voiceRecorder", () => ({
  VOICE_MIME_TYPE: "audio/mp4",
  requestMicrophone: jest.fn(async () => "granted"),
  startVoiceCapture: jest.fn(async () => mockCapture),
}));
jest.mock("../ui/haptics", () => ({ lightTapHaptic: jest.fn() }));
const mockIds: string[] = [];
jest.mock("expo-crypto", () => ({
  randomUUID: () => mockIds.shift() ?? "00000000-0000-4000-8000-000000000000",
}));
jest.mock("expo-file-system", () => ({
  File: class {
    delete() {}
  },
}));

function shared() {
  return {
    files: {
      adopt: jest.fn(async () => 3000),
      adoptVisual: jest.fn(async () => undefined),
      remove: jest.fn(async () => undefined),
    },
    queue: { add: jest.fn(async () => undefined) },
  };
}

function client(available = true) {
  return {
    voice: {
      config: jest.fn(async () => ({
        available,
        reason: available ? "" : "Connect a speech provider",
        models: [],
        default_model: "",
        cleanup_model: "",
        max_duration_ms: 600_000,
        max_audio_bytes: 8_000_000,
      })),
    },
  } as unknown as OpenCuriaClient;
}

async function record(files = shared(), connection = client()) {
  const notify = jest.fn();
  let started = false;
  await act(async () => {
    started = Boolean(
      await startRecording({
        owner: Symbol("test"),
        scope: SCOPE,
        shared: files as never,
        client: connection,
        notify,
      }),
    );
  });
  return { started, files, notify };
}

const LINES = ["const total = 1;", "export default total;"];
const IMAGE: VisualSurfaceSpec = {
  kind: "image",
  path: "/workspace/screens/login.png",
  width: 1600,
  height: 800,
};
const FILE: VisualSurfaceSpec = {
  kind: "file",
  path: "/workspace/src/total.ts",
  lines: LINES,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockIds.splice(0, mockIds.length, RECORDING, "clip-1");
  activeRecording()?.cancel();
});

describe("voice recordings", () => {
  it("keeps the pictures and file lines shown meanwhile, without a single screenshot", async () => {
    const { started, files } = await record();
    expect(started).toBe(true);
    const state = renderHook(() => ({
      recording: useActiveRecording(),
      pointing: usePointMode(),
    }));
    const viewer = renderHook(
      ({ spec }: { spec: VisualSurfaceSpec | null }) =>
        useRecordingSurface(spec),
      { initialProps: { spec: FILE } },
    );
    expect(state.result.current.recording).toMatchObject({
      id: RECORDING,
      phase: "recording",
      mode: "use",
      surface: { kind: "file", pointable: true },
    });
    act(() => {
      viewer.result.current!.view([0, 1, 100, 1]);
      viewer.result.current!.pointer({
        event: "down",
        point: { x: 2, y: 1 },
        mode: "use",
      });
      viewer.result.current!.pointer({
        event: "up",
        point: { x: 2, y: 1 },
        mode: "use",
      });
    });
    // A picture opens above the file and takes over.
    const picture = renderHook(() => useRecordingSurface(IMAGE));
    expect(state.result.current.recording?.surface?.kind).toBe("image");
    // Like the viewer: the whole picture shows.
    act(() => picture.result.current!.view());
    act(() => setRecordingMode("point"));
    expect(state.result.current.pointing).toBe(true);
    act(() => {
      picture.result.current!.pointer({
        event: "down",
        point: { x: 400, y: 300 },
        mode: "point",
      });
      picture.result.current!.pointer({
        event: "up",
        point: { x: 420, y: 310 },
        mode: "point",
      });
    });
    picture.unmount();
    expect(state.result.current.recording?.surface?.kind).toBe("file");
    await act(async () => finishRecording());

    const [id, json, frames] = files.files.adoptVisual.mock.calls[0]!;
    expect(id).toBe("clip-1");
    expect(frames).toEqual([]);
    const timeline = JSON.parse(json as string);
    expect(timeline.frames).toEqual([]);
    expect(
      timeline.views.map(
        (view: { target: { kind: string } | null }) =>
          view.target?.kind ?? null,
      ),
    ).toEqual(["file", "image", "file"]);
    expect(timeline.views[0].region).toEqual([0, 1, 21, 1]);
    expect(timeline.pointers[0]).toMatchObject({
      event: "down",
      x: 2,
      y: 1,
      target: {
        kind: "file",
        path: "/workspace/src/total.ts",
        width: 21,
        height: 2,
      },
      element: { role: "line", text: "export default total;" },
    });
    expect(timeline.pointers[2]).toMatchObject({
      event: "down",
      x: 400,
      y: 300,
      mode: "point",
      target: IMAGE,
    });
    expect(timeline.app).toBeUndefined();
    expect(files.queue.add).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "clip-1",
        target: SCOPE,
        send: false,
        visual: { kind: "pointing", recordingId: RECORDING },
      }),
    );
    expect(state.result.current.recording).toBeNull();
  });

  it("stays a plain dictation when nothing was shown", async () => {
    const { files } = await record();
    await act(async () => activeRecording()!.finish(true));
    expect(files.files.adoptVisual).not.toHaveBeenCalled();
    const clip = files.queue.add.mock.calls[0]![0] as Record<string, unknown>;
    expect(clip).toMatchObject({ id: "clip-1", send: true });
    expect(clip.visual).toBeUndefined();
  });

  it("runs one recording at a time and discards it on request", async () => {
    const first = await record();
    const second = await record();
    expect(first.started).toBe(true);
    expect(second.started).toBe(false);
    act(() => cancelRecording());
    expect(mockCapture.cancel).toHaveBeenCalled();
    expect(activeRecording()).toBeNull();
    expect(first.files.queue.add).not.toHaveBeenCalled();
  });

  it("stops at once and says why when voice input is not set up", async () => {
    const { notify } = await record(shared(), client(false));
    await act(async () => undefined);
    expect(activeRecording()).toBeNull();
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        tone: "error",
        message: "Voice input isn’t set up. Connect a speech provider.",
      }),
    );
  });

  it("keeps a recording stopped at the limit within it", async () => {
    // The recorder reports a few milliseconds past ten minutes.
    mockCapture.stop.mockResolvedValueOnce({
      uri: "file:///cache/rec.m4a",
      durationMs: 600_087.4,
    });
    const { files } = await record();
    await act(async () => finishRecording());
    expect(files.queue.add).toHaveBeenCalledWith(
      expect.objectContaining({ durationMs: 600_000 }),
    );
  });

  it("rejects a recording too short to hold speech", async () => {
    mockCapture.stop.mockResolvedValueOnce({
      uri: "file:///cache/rec.m4a",
      durationMs: 150,
    });
    const { files, notify } = await record();
    await act(async () => finishRecording());
    expect(files.files.remove).toHaveBeenCalledWith({ id: "clip-1" });
    expect(files.queue.add).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "error" }),
    );
  });
});
