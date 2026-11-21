import { act, renderHook, waitFor } from "@testing-library/react-native";
import type { ChatInputScope } from "@opencuria/chat-core/chatInputCache";
import type { ChatVoiceClip } from "./voiceClipStore";
import {
  useVoiceDictation,
  type VoiceDictationOptions,
} from "./useVoiceDictation";

const mockCapture = {
  sample: jest.fn(() => ({ durationMs: 0, decibels: -30, recording: true })),
  stop: jest.fn(async () => ({
    uri: "file:///cache/rec.m4a",
    durationMs: 4200,
  })),
  cancel: jest.fn(async () => undefined),
  onInterrupted: jest.fn(() => () => undefined),
};
const mockRecorder = {
  requestMicrophone: jest.fn(async () => "granted"),
  startVoiceCapture: jest.fn(async () => mockCapture),
};
jest.mock("./voiceRecorder", () => ({
  VOICE_MIME_TYPE: "audio/mp4",
  requestMicrophone: () => mockRecorder.requestMicrophone(),
  startVoiceCapture: () => mockRecorder.startVoiceCapture(),
}));

/** Recordings saved "on the device", shared by every store of the test. */
const mockSaved: { clips: ChatVoiceClip[]; audio: Map<string, number> } = {
  clips: [],
  audio: new Map(),
};
jest.mock("./voiceClipStore", () => ({
  voiceClipFiles: () => ({
    audioUri: async (clip: { id: string }) => `file:///voice/${clip.id}.m4a`,
    adopt: async (id: string) => {
      mockSaved.audio.set(id, 3000);
      return 3000;
    },
    load: async () => mockSaved.clips,
    save: async (clips: ChatVoiceClip[]) => {
      mockSaved.clips = clips;
    },
    remove: async (clip: { id: string }) => {
      mockSaved.audio.delete(clip.id);
    },
  }),
}));
jest.mock("expo-file-system", () => ({
  File: class {
    mockUri: string;
    constructor(mockUri: string) {
      this.mockUri = mockUri;
    }
    async base64() {
      return `audio:${this.mockUri}`;
    }
  },
}));
let mockIds = 0;
jest.mock("expo-crypto", () => ({ randomUUID: () => `clip-${++mockIds}` }));
jest.mock("../ui/haptics", () => ({ lightTapHaptic: jest.fn() }));

const mockNetwork = {
  online: true,
  listeners: new Set<(online: boolean) => void>(),
  set(online: boolean) {
    this.online = online;
    for (const listener of [...this.listeners]) listener(online);
  },
};
jest.mock("./voiceConnectivity", () => ({
  AppVoiceConnectivity: class {
    online() {
      return mockNetwork.online;
    }
    setServerReachable() {}
    watch(listener: (online: boolean) => void) {
      mockNetwork.listeners.add(listener);
      return () => mockNetwork.listeners.delete(listener);
    }
    dispose() {}
  },
}));

const chatA: ChatInputScope = { workspaceId: "ws-1", sessionId: "a" };
const chatB: ChatInputScope = { workspaceId: "ws-1", sessionId: "b" };

function client() {
  return {
    voice: {
      config: jest.fn(async () => ({
        available: true,
        models: [],
        default_model: "openrouter/scribe",
        cleanup_model: "openrouter/small",
        max_duration_ms: 600_000,
        max_audio_bytes: 8_388_608,
      })),
      transcribe: jest.fn(async () => ({
        text: "Fix the login.",
        segments: [],
        timing: "approximate",
      })),
    },
  };
}

function setup(overrides: Partial<VoiceDictationOptions> = {}) {
  const api = client();
  const editDraft = jest.fn(async () => undefined);
  const flushDrafts = jest.fn(async () => undefined);
  const onOpenSettings = jest.fn();
  const view = renderHook(
    (props: Partial<VoiceDictationOptions>) =>
      useVoiceDictation({
        client: api as never,
        account: "server|org|user",
        offline: false,
        scope: chatA,
        editDraft,
        flushDrafts,
        onOpenSettings,
        ...overrides,
        ...props,
      }),
    { initialProps: {} },
  );
  const received: Array<[string, boolean]> = [];
  act(() => {
    view.result.current!.receive((text, send) => received.push([text, send]));
  });
  return { view, api, editDraft, flushDrafts, onOpenSettings, received };
}

async function record(view: ReturnType<typeof setup>["view"]) {
  await act(async () => view.result.current!.start());
  await waitFor(() =>
    expect(view.result.current!.recording?.phase).toBe("recording"),
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockIds = 0;
  mockSaved.clips = [];
  mockSaved.audio.clear();
  mockNetwork.online = true;
  mockNetwork.listeners.clear();
  mockRecorder.requestMicrophone.mockResolvedValue("granted");
});
afterEach(() => {
  jest.useRealTimers();
});

it("records, saves the file and puts the text into the draft on screen", async () => {
  jest.useFakeTimers();
  const { view, api, flushDrafts, received } = setup();
  await record(view);
  mockCapture.sample.mockReturnValue({
    durationMs: 1500,
    decibels: -20,
    recording: true,
  });
  await act(async () => {
    jest.advanceTimersByTime(300);
  });
  expect(view.result.current!.recording).toMatchObject({ elapsedMs: 1500 });
  expect(view.result.current!.recording!.levels.length).toBe(3);

  await act(async () => view.result.current!.finish(false));
  await waitFor(() => expect(received).toEqual([["Fix the login.", false]]));
  expect(api.voice.transcribe).toHaveBeenCalledWith(
    "ws-1",
    {
      audio_base64: "audio:file:///voice/clip-1.m4a",
      mime_type: "audio/mp4",
      duration_ms: 4200,
    },
    expect.anything(),
  );
  expect(flushDrafts).toHaveBeenCalled();
  expect(view.result.current!.recording).toBeNull();
  await waitFor(() => expect(view.result.current!.clips).toEqual([]));
  expect(mockSaved.clips).toEqual([]);
  expect(mockSaved.audio.size).toBe(0);
});

it("keeps an offline recording and transcribes it once the connection is back", async () => {
  mockNetwork.online = false;
  const { view, api, received } = setup({ offline: true });
  await record(view);
  await act(async () => view.result.current!.finish(true));
  await waitFor(() =>
    expect(view.result.current!.clips[0]?.status).toMatchObject({
      kind: "waiting",
      failure: { kind: "offline" },
    }),
  );
  expect(mockSaved.clips.map((clip) => clip.id)).toEqual(["clip-1"]);
  expect(api.voice.transcribe).not.toHaveBeenCalled();

  act(() => mockNetwork.set(true));
  // "Stop and send" still sends: its chat is on screen.
  await waitFor(() => expect(received).toEqual([["Fix the login.", true]]));
  await waitFor(() => expect(view.result.current!.clips).toEqual([]));
});

it("finishes a recording when its chat is left and gives the text to that chat's draft", async () => {
  const { view, editDraft, received } = setup();
  await record(view);
  view.rerender({ scope: chatB });
  await waitFor(() => expect(editDraft).toHaveBeenCalledTimes(1));
  expect(mockCapture.stop).toHaveBeenCalledTimes(1);
  expect(mockCapture.cancel).not.toHaveBeenCalled();
  expect(received).toEqual([]);
  const [scope, update] = editDraft.mock.calls[0] as unknown as [
    ChatInputScope,
    (text: string) => string,
  ];
  expect(scope).toEqual(chatA);
  expect(update("Earlier draft")).toBe("Earlier draft\nFix the login.");
});

it("explains a blocked microphone and offers the system settings", async () => {
  mockRecorder.requestMicrophone.mockResolvedValue("blocked");
  const { view } = setup();
  await act(async () => view.result.current!.start());
  expect(view.result.current!.recording).toBeNull();
  expect(view.result.current!.notice).toMatchObject({
    tone: "error",
    message: expect.stringMatching(/denied/),
    action: { label: "Open Settings" },
  });
  expect(mockRecorder.startVoiceCapture).not.toHaveBeenCalled();
});

it("stops at once when voice input is not set up and links to its settings", async () => {
  const { view, api, onOpenSettings } = setup();
  api.voice.config.mockResolvedValueOnce({
    available: false,
    reason:
      "Connect an OpenRouter or OpenAI-compatible account in provider settings",
  } as never);
  await act(async () => view.result.current!.start());
  await waitFor(() => expect(view.result.current!.notice).not.toBeNull());
  expect(view.result.current!.recording).toBeNull();
  expect(mockCapture.cancel).toHaveBeenCalled();
  expect(view.result.current!.notice!.message).toMatch(/isn’t set up/);
  act(() => view.result.current!.notice!.action!.onPress());
  expect(onOpenSettings).toHaveBeenCalledTimes(1);
});

it("rejects a recording too short to hold speech without sending it", async () => {
  mockCapture.stop.mockResolvedValueOnce({
    uri: "file:///cache/rec.m4a",
    durationMs: 120,
  });
  const { view, api } = setup();
  await record(view);
  await act(async () => view.result.current!.finish(false));
  expect(view.result.current!.notice?.message).toMatch(/too short/);
  expect(api.voice.transcribe).not.toHaveBeenCalled();
  expect(mockSaved.clips).toEqual([]);
  expect(mockSaved.audio.size).toBe(0);
});

it("brings recordings saved before a restart back to their chat", async () => {
  mockSaved.clips = [
    {
      id: "saved",
      workspaceId: "ws-1",
      target: chatB,
      mimeType: "audio/mp4",
      durationMs: 2000,
      bytes: 3000,
      createdAt: new Date().toISOString(),
      send: true,
    },
  ];
  mockNetwork.online = false;
  const { view, editDraft } = setup({ scope: chatB });
  await waitFor(() =>
    expect(view.result.current!.clips.map((clip) => clip.id)).toEqual([
      "saved",
    ]),
  );
  view.rerender({ scope: chatA });
  expect(view.result.current!.clips).toEqual([]);
  act(() => mockNetwork.set(true));
  // A restored recording never sends; its text waits in the chat's draft.
  await waitFor(() => expect(editDraft).toHaveBeenCalledTimes(1));
});
