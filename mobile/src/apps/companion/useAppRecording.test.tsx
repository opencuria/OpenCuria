import { act, renderHook, waitFor } from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import type { AppEventRecord, AppTouchEvent } from "./companionNative";
import { useAppRecording } from "./useAppRecording";

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
jest.mock("../../voice/voiceRecorder", () => ({
  VOICE_MIME_TYPE: "audio/mp4",
  requestMicrophone: jest.fn(async () => "granted"),
  startVoiceCapture: jest.fn(async () => mockCapture),
}));

const mockShared = {
  files: {
    adopt: jest.fn(async () => 3000),
    adoptVisual: jest.fn(async () => undefined),
    remove: jest.fn(async () => undefined),
  },
  queue: { add: jest.fn(async () => undefined) },
};
jest.mock("../../voice/voiceQueue", () => ({
  sharedVoiceQueue: () => mockShared,
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => mockIds.next() }));
const mockIds = {
  values: [] as string[],
  next(): string {
    return this.values.shift() ?? "00000000-0000-4000-8000-000000000000";
  },
};
const mockDeleted: string[] = [];
jest.mock("expo-file-system", () => ({
  File: class {
    mockUri: string;
    constructor(mockUri: string) {
      this.mockUri = mockUri;
    }
    delete() {
      mockDeleted.push(this.mockUri);
    }
  },
}));

const mockNative = {
  touch: null as ((event: AppTouchEvent) => void) | null,
  event: null as ((event: AppEventRecord) => void) | null,
  frames: 0,
  start: jest.fn(async () => ({
    width: 1080,
    height: 2400,
    platform: "android",
  })),
  stop: jest.fn(async () => undefined),
  recent: jest.fn(async () => [] as AppEventRecord[]),
};
jest.mock("./companionNative", () => ({
  startAppObservation: () => mockNative.start(),
  stopAppObservation: () => mockNative.stop(),
  recentAppEvents: () => mockNative.recent(),
  captureAppFrame: jest.fn(async (_id: string, index: number) => ({
    uri: `file:///cache/opencuria-app-recordings/${index}.jpg`,
    width: 540,
    height: 1200,
    frameId: `frame-${(mockNative.frames += 1)}`,
    capturedAt: Date.now(),
    texts: ["Todos", "Add"],
  })),
  onAppTouch: (listener: (event: AppTouchEvent) => void) => {
    mockNative.touch = listener;
    return { remove: () => (mockNative.touch = null) };
  },
  onAppEvent: (listener: (event: AppEventRecord) => void) => {
    mockNative.event = listener;
    return { remove: () => (mockNative.event = null) };
  },
}));

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

function hook(connection = client()) {
  return renderHook(() =>
    useAppRecording({
      client: connection,
      account: "https://curia.test/api/v1|org-a|7",
      app: { app_id: "app-1", name: "Todo Demo", platform: "android" },
    }),
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockNative.frames = 0;
  mockDeleted.length = 0;
  mockIds.values = [RECORDING, "clip-1"];
});

describe("useAppRecording", () => {
  it("keeps the app's screen, taps and logs on the audio's timeline and queues it for the chat", async () => {
    mockNative.recent.mockResolvedValueOnce([
      {
        t: Date.now() - 3_000,
        kind: "log",
        level: "warn",
        message: "Slow start",
      },
    ]);
    const { result } = hook();
    await act(async () => {
      expect(await result.current.start(SCOPE)).toBe(true);
    });
    expect(result.current.state.phase).toBe("recording");
    await waitFor(() =>
      expect(result.current.meter.getSnapshot().elapsedMs).toBe(1200),
    );
    await act(async () => {
      mockNative.touch?.({
        t: Date.now(),
        phase: "down",
        x: 540,
        y: 1200,
        mode: "point",
        element: {
          role: "button",
          text: "Add",
          test_id: "add-button",
          interactive: true,
          bounds: [0, 1080, 1080, 240],
        },
      });
      mockNative.event?.({
        t: Date.now(),
        kind: "network",
        method: "GET",
        url: "http://api.opencuria.local:8080/todos?key=1",
        status: 500,
        durationMs: 120,
      });
    });
    await act(async () => {
      mockNative.touch?.({
        t: Date.now(),
        phase: "up",
        x: 540,
        y: 1200,
        mode: "point",
      });
    });
    await act(async () => {
      await result.current.finish();
    });

    expect(mockShared.queue.add).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "clip-1",
        target: SCOPE,
        durationMs: 4200,
        visual: { kind: "app", recordingId: RECORDING },
      }),
    );
    const [id, json, frames] = mockShared.files.adoptVisual.mock.calls[0]!;
    expect(id).toBe("clip-1");
    expect(frames.length).toBeGreaterThan(0);
    const timeline = JSON.parse(json as string);
    const press = timeline.pointers.find(
      (pointer: { event: string }) => pointer.event === "down",
    );
    expect(press).toMatchObject({
      x: 270,
      y: 600,
      mode: "point",
      target: { kind: "app", width: 540, height: 1200 },
      element: {
        role: "button",
        test_id: "add-button",
        bounds: [0, 540, 540, 120],
      },
    });
    expect(press.target.path).toMatch(
      new RegExp(
        `/workspace/.opencuria/recordings/${RECORDING}/frames/\\d{6}\\.jpg`,
      ),
    );
    expect(timeline.frames[0].texts).toEqual(["Todos", "Add"]);
    expect(timeline.events).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "log",
          message: expect.stringMatching(
            /^\(3\.\d s before recording\) Slow start$/,
          ),
        }),
        expect.objectContaining({
          kind: "network",
          url: "http://api.opencuria.local:8080/todos?key=…",
          status: 500,
        }),
      ]),
    );
    expect(timeline.app.name).toBe("Todo Demo");
    expect(mockNative.stop).toHaveBeenCalled();
    expect(result.current.state.phase).toBe("idle");
  });

  it("deletes the frames of a discarded recording", async () => {
    const { result } = hook();
    await act(async () => {
      await result.current.start(SCOPE);
    });
    await waitFor(() => expect(mockNative.frames).toBeGreaterThan(0));
    act(() => result.current.cancel());
    expect(mockCapture.cancel).toHaveBeenCalled();
    expect(mockNative.stop).toHaveBeenCalled();
    expect(mockDeleted).toEqual([
      "file:///cache/opencuria-app-recordings/0.jpg",
    ]);
    expect(mockShared.queue.add).not.toHaveBeenCalled();
  });

  it("says so instead of recording when voice input is not set up", async () => {
    const { result } = hook(client(false));
    await act(async () => {
      expect(await result.current.start(SCOPE)).toBe(false);
    });
    expect(mockNative.start).not.toHaveBeenCalled();
    expect(result.current.state.notice?.message).toBe(
      "Voice input isn’t set up. Connect a speech provider.",
    );
  });
});
