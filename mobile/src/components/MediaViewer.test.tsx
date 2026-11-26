import React from "react";
import { Image } from "react-native";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { State } from "react-native-gesture-handler";
import {
  fireGestureHandler,
  getByGestureTestId,
} from "react-native-gesture-handler/jest-utils";
import { ThemeProvider } from "../ui/theme";
import {
  activeRecording,
  setRecordingMode,
  startRecording,
} from "../voice/voiceRecording";
import { MediaViewer } from "./MediaViewer";

jest.mock("lucide-react-native", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const icon = (props: Record<string, unknown>) =>
    React.createElement(ReactNative.View, props);
  return new Proxy({}, { get: () => icon });
});
jest.mock("../voice/voiceRecorder", () => ({
  VOICE_MIME_TYPE: "audio/mp4",
  requestMicrophone: jest.fn(async () => "granted"),
  startVoiceCapture: jest.fn(async () => ({
    sample: () => ({ durationMs: 1200, decibels: -30, recording: true }),
    stop: async () => ({ uri: "file:///cache/rec.m4a", durationMs: 4200 }),
    cancel: async () => undefined,
    onInterrupted: () => () => undefined,
  })),
}));
jest.mock("../ui/haptics", () => ({ lightTapHaptic: jest.fn() }));
jest.mock("expo-crypto", () => ({
  randomUUID: () => "6a1d0f6e-6f8a-4c5e-9a0b-3d2f1e4c5b6a",
}));

const PICTURE = {
  kind: "image" as const,
  path: "/workspace/login.png",
  label: "Login",
};

async function recording() {
  const files = {
    files: {
      adopt: jest.fn(async () => 3000),
      adoptVisual: jest.fn(async () => undefined),
      remove: jest.fn(async () => undefined),
    },
    queue: { add: jest.fn(async () => undefined) },
  };
  await act(async () => {
    await startRecording({
      owner: Symbol("test"),
      scope: { workspaceId: "ws" },
      shared: files as never,
      client: null,
      notify: jest.fn(),
    });
  });
  return files;
}

async function open(items = [PICTURE]) {
  const resolveMedia = jest.fn(async (path: string) => ({
    uri: `file:///cache${path}`,
    release: jest.fn(),
  }));
  render(
    <ThemeProvider>
      <MediaViewer
        gallery={{ items, index: 0 }}
        resolveMedia={resolveMedia}
        scope="scope"
        onClose={jest.fn()}
      />
    </ThemeProvider>,
  );
  const stage = await screen.findByTestId("image-preview");
  // A 400 × 800 point viewer: the 1600 × 800 picture fills 400 × 200 at y 300.
  fireEvent(stage, "layout", {
    nativeEvent: { layout: { x: 0, y: 0, width: 400, height: 800 } },
  });
  return stage;
}

const tap = (x: number, y: number) =>
  fireGestureHandler(getByGestureTestId("image-preview-tap"), [
    { state: State.BEGAN, x, y },
    { state: State.ACTIVE, x, y },
    { state: State.END, x, y },
  ]);

// The files' own sizes; image views report the size they decoded at.
const SIZES: Record<string, [number, number]> = {
  "file:///cache/workspace/login.png": [1600, 800],
  "https://example.com/cat.png": [100, 100],
};
beforeEach(() => {
  jest
    .spyOn(Image, "getSize")
    .mockImplementation(
      (uri: string, success: (w: number, h: number) => void) => {
        const size = SIZES[uri];
        if (size) success(...size);
      },
    );
});
afterEach(() => {
  activeRecording()?.cancel();
  jest.restoreAllMocks();
});

it("keeps the picture's pixels a finger taps or points at and floats the recording bar over it", async () => {
  const files = await recording();
  await open();
  expect(activeRecording()!.state.surface).toEqual({
    kind: "image",
    pointable: true,
  });
  // The view decodes the picture smaller (and again later); the recording
  // keeps the file's own pixels and its one view of it.
  const image = screen.UNSAFE_getByProps({ contentFit: "contain" });
  act(() => image.props.onLoad({ source: { width: 1080, height: 540 } }));
  act(() => image.props.onLoad({ source: { width: 720, height: 360 } }));
  // The composer is out of sight: the bar with Use and Point is here.
  expect(screen.getByTestId("recording-bar")).toBeTruthy();
  expect(screen.getByTestId("recording-mode")).toBeTruthy();

  // Use: a tap marks a spot (and toggles the viewer's bars).
  await act(async () => tap(100, 350));
  // Point: the laser shows a spot without paging or zooming.
  act(() => setRecordingMode("point"));
  await screen.findByTestId("image-preview-pointer");
  await act(async () =>
    fireGestureHandler(getByGestureTestId("image-preview-pointer"), [
      { state: State.BEGAN, x: 200, y: 400 },
      { state: State.ACTIVE, x: 300, y: 420 },
      { state: State.END, x: 300, y: 420 },
    ]),
  );
  await act(async () => activeRecording()!.finish());

  const timeline = JSON.parse(
    files.files.adoptVisual.mock.calls[0]![1] as string,
  );
  const target = {
    kind: "image",
    path: "/workspace/login.png",
    width: 1600,
    height: 800,
  };
  expect(timeline.frames).toEqual([]);
  expect(timeline.views).toEqual([expect.objectContaining({ target })]);
  expect(
    timeline.pointers.map(
      (pointer: { event: string; x: number; y: number; mode: string }) => [
        pointer.event,
        pointer.x,
        pointer.y,
        pointer.mode,
      ],
    ),
  ).toEqual([
    ["down", 400, 200, "use"],
    ["up", 400, 200, "use"],
    ["down", 800, 400, "point"],
    // Moves come at most every 100 ms; this one followed too soon.
    ["up", 1200, 480, "point"],
  ]);
});

it("shows the bar without modes for a picture that has no workspace path", async () => {
  await recording();
  await open([
    { kind: "image", uri: "https://example.com/cat.png", label: "Cat" },
  ]);
  const image = screen.UNSAFE_getByProps({ contentFit: "contain" });
  act(() => image.props.onLoad({ source: { width: 100, height: 100 } }));
  expect(activeRecording()!.state.surface).toBeNull();
  expect(screen.getByTestId("recording-bar")).toBeTruthy();
  expect(screen.queryByTestId("recording-mode")).toBeNull();
});

it("shows no bar while nothing records", async () => {
  await open();
  expect(screen.queryByTestId("recording-bar")).toBeNull();
});
