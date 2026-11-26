import { act, renderHook, waitFor } from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import {
  activeRecording,
  startRecording,
  type VoiceRecording,
} from "../../voice/voiceRecording";
import type { AppEventRecord, AppTouchEvent } from "./companionNative";
import { useAppSurface } from "./useAppSurface";

const RECORDING = "6a1d0f6e-6f8a-4c5e-9a0b-3d2f1e4c5b6a";
const SCOPE = { workspaceId: "ws-1", sessionId: "chat-1", harnessId: "native" };
const APP = {
  app_id: "app-1",
  name: "Todo Demo",
  platform: "android" as const,
};

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
jest.mock("../../ui/haptics", () => ({ lightTapHaptic: jest.fn() }));
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
  screen: "list",
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
  captureAppFrame: jest.fn(
    async (_id: string, index: number, previous: string | null) => {
      if (previous === mockNative.screen)
        return { unchanged: true, capturedAt: Date.now() };
      mockNative.frames += 1;
      return {
        uri: `file:///cache/opencuria-app-recordings/${index}.jpg`,
        width: 540,
        height: 1200,
        frameId: mockNative.screen,
        capturedAt: Date.now(),
        texts: ["Todos", "Add"],
      };
    },
  ),
  onAppTouch: (listener: (event: AppTouchEvent) => void) => {
    mockNative.touch = listener;
    return { remove: () => (mockNative.touch = null) };
  },
  onAppEvent: (listener: (event: AppEventRecord) => void) => {
    mockNative.event = listener;
    return { remove: () => (mockNative.event = null) };
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
  let recording: VoiceRecording | null = null;
  await act(async () => {
    recording = await startRecording({
      owner: Symbol("test"),
      scope: SCOPE,
      shared: files as never,
      client: connection,
      notify,
    });
  });
  return { recording: recording as VoiceRecording | null, files, notify };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockNative.frames = 0;
  mockNative.screen = "list";
  mockDeleted.length = 0;
  mockIds.values = [RECORDING, "clip-1"];
  activeRecording()?.cancel();
});

describe("useAppSurface", () => {
  it("keeps the app's screen, taps and logs on the recording's timeline while the app shows", async () => {
    mockNative.recent.mockResolvedValueOnce([
      {
        t: Date.now() - 3_000,
        kind: "log",
        level: "warn",
        message: "Slow start",
      },
    ]);
    const { recording, files } = await record();
    expect(recording?.id).toBe(RECORDING);
    const view = renderHook(({ shown }) => useAppSurface(shown, APP), {
      initialProps: { shown: true },
    });
    await waitFor(() => expect(mockNative.frames).toBe(1));
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
    // The sheet covers the app: its touches are not the recording's.
    view.rerender({ shown: false });
    await act(async () => {
      mockNative.touch?.({
        t: Date.now(),
        phase: "down",
        x: 1,
        y: 1,
        mode: "point",
      });
    });
    // Back on the app, its screen is captured again; it changed meanwhile.
    mockNative.screen = "details";
    view.rerender({ shown: true });
    await waitFor(() => expect(mockNative.frames).toBe(2));
    await act(async () => {
      await recording!.finish();
    });

    expect(files.queue.add).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "clip-1",
        target: SCOPE,
        durationMs: 4200,
        visual: { kind: "app", recordingId: RECORDING },
      }),
    );
    const [id, json, frames] = files.files.adoptVisual.mock.calls[0]!;
    expect(id).toBe("clip-1");
    expect(frames).toEqual([
      "file:///cache/opencuria-app-recordings/0.jpg",
      "file:///cache/opencuria-app-recordings/1.jpg",
    ]);
    const timeline = JSON.parse(json as string);
    const presses = timeline.pointers.filter(
      (pointer: { event: string }) => pointer.event === "down",
    );
    expect(presses).toHaveLength(1);
    expect(presses[0]).toMatchObject({
      x: 270,
      y: 600,
      mode: "point",
      target: {
        kind: "app",
        path: `/workspace/.opencuria/recordings/${RECORDING}/frames/000000.jpg`,
        width: 540,
        height: 1200,
      },
      element: {
        role: "button",
        test_id: "add-button",
        bounds: [0, 540, 540, 120],
      },
    });
    expect(timeline.frames[0].texts).toEqual(["Todos", "Add"]);
    expect(timeline.events).toEqual([
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
    ]);
    expect(timeline.app).toEqual(APP);
    // One observation for the whole recording, stopped as it ends.
    expect(mockNative.start).toHaveBeenCalledTimes(1);
    expect(mockNative.stop).toHaveBeenCalled();
    expect(activeRecording()).toBeNull();
  });

  it("deletes the frames of a discarded recording", async () => {
    const { recording, files } = await record();
    renderHook(() => useAppSurface(true, APP));
    await waitFor(() => expect(mockNative.frames).toBe(1));
    await act(async () => recording!.cancel());
    expect(mockCapture.cancel).toHaveBeenCalled();
    await waitFor(() => expect(mockNative.stop).toHaveBeenCalled());
    expect(mockDeleted).toEqual([
      "file:///cache/opencuria-app-recordings/0.jpg",
    ]);
    expect(files.queue.add).not.toHaveBeenCalled();
  });

  it("observes nothing while no recording runs", () => {
    renderHook(() => useAppSurface(true, APP));
    expect(mockNative.start).not.toHaveBeenCalled();
  });
});
