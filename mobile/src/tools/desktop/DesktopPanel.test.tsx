import React from "react";
import { StyleSheet } from "react-native";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { ThemeProvider } from "../../ui/theme";
import { DesktopPanel } from "./DesktopPanel";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("expo-crypto", () => ({
  randomUUID: () => "11111111-2222-4333-8444-555555555555",
}));
jest.mock("expo-clipboard", () => ({
  getStringAsync: jest.fn(async () => "from phone"),
  setStringAsync: jest.fn(async () => true),
}));

const mockInjected: string[] = [];
let mockPageMessage: ((data: string) => void) | null = null;
jest.mock("react-native-webview", () => {
  const React = require("react");
  const { View } = require("react-native");
  return {
    WebView: React.forwardRef(
      (
        props: { source: { uri: string }; onMessage: (event: unknown) => void },
        ref: React.Ref<unknown>,
      ) => {
        React.useImperativeHandle(ref, () => ({
          injectJavaScript: (script: string) => mockInjected.push(script),
        }));
        mockPageMessage = (data: string) =>
          props.onMessage({ nativeEvent: { data } });
        return (
          <View testID="mock-webview" accessibilityHint={props.source.uri} />
        );
      },
    ),
  };
});

type Handler = (event: Record<string, unknown>) => void;

function heldStatus(revision: number) {
  return {
    active: true,
    proxy_url: "/ws/desktop/ws/",
    viewer_held: true,
    computer_use_active: false,
    viewer_lease_state: "held",
    revision,
    epoch: "epoch",
  };
}

function fakeClient() {
  const handlers = new Map<string, Set<Handler>>();
  let revision = 0;
  const desktop = {
    start: jest.fn(async (_id: string, intent: { intent_revision: number }) => {
      revision = intent.intent_revision;
      return { task_id: "start" };
    }),
    stop: jest.fn(async () => ({ task_id: "stop" })),
    renew: jest.fn(async () => heldStatus(revision)),
    status: jest.fn(async () =>
      revision ? heldStatus(revision) : { ...heldStatus(0), active: false },
    ),
    takeControl: jest.fn(async () => ({ aborted_session_ids: [] })),
    readClipboard: jest.fn(async () => ({ text: "from desktop" })),
    writeClipboard: jest.fn(async (_id: string, text: string) => ({ text })),
    viewerUrl: jest.fn(
      (proxy: string, intent: { intent_revision: number }) =>
        `https://curia.test${proxy}?intent_revision=${intent.intent_revision}`,
    ),
  };
  const client = {
    desktop,
    workspaces: {
      get: jest.fn(async () => ({ desktop_width: 1280, desktop_height: 720 })),
    },
    realtime: {
      subscribeWorkspace: jest.fn(() => () => undefined),
      on: jest.fn((event: string, handler: Handler) => {
        const set = handlers.get(event) ?? new Set();
        set.add(handler);
        handlers.set(event, set);
        return () => set.delete(handler);
      }),
      onReconnect: jest.fn(() => () => undefined),
    },
  } as unknown as OpenCuriaClient;
  const emit = (event: string, data: Record<string, unknown>) =>
    handlers.get(event)?.forEach((handler) => handler(data));
  return { client, desktop, emit };
}

async function flush() {
  for (let index = 0; index < 6; index += 1)
    await act(async () => {
      await Promise.resolve();
    });
}

async function renderConnected() {
  const fake = fakeClient();
  render(
    <ThemeProvider>
      <DesktopPanel
        client={fake.client}
        workspaceId="ws"
        workspaceName="demo"
        active
        media={null}
        bottomInset={0}
      />
    </ThemeProvider>,
  );
  fireEvent(
    screen.getByTestId("desktop-panel").children[1] as never,
    "layout",
    {
      nativeEvent: { layout: { width: 390, height: 700 } },
    },
  );
  await flush();
  act(() => mockPageMessage?.('{"type":"state","state":"connected"}'));
  return fake;
}

beforeEach(() => {
  // Lease renewals and release grace periods must not outlive a test.
  jest.useFakeTimers({
    doNotFake: ["queueMicrotask", "nextTick", "setImmediate"],
  });
  mockInjected.length = 0;
  mockPageMessage = null;
});

afterEach(() => {
  cleanup();
  jest.clearAllTimers();
  jest.useRealTimers();
});

it("starts the desktop when shown and loads the viewer once the lease is held", async () => {
  const fake = await renderConnected();
  expect(fake.desktop.start).toHaveBeenCalledWith(
    "ws",
    {
      viewer_client_id: "11111111-2222-4333-8444-555555555555",
      intent_revision: 1,
    },
    expect.anything(),
  );
  expect(screen.getByTestId("mock-webview").props.accessibilityHint).toBe(
    "https://curia.test/ws/desktop/ws/?intent_revision=1",
  );
  expect(screen.getByTestId("desktop-status").props.children).toBe("Connected");
  // The remote pointer is synced to the native cursor once connected.
  expect(mockInjected.at(-1)).toContain(".pointer(960,540,0,");
  expect(screen.getByTestId("desktop-show-keyboard")).toBeTruthy();
});

it("sends typed text and backspaces from the hidden keyboard input", async () => {
  await renderConnected();
  const input = screen.getByTestId("desktop-keyboard-input", {
    includeHiddenElements: true,
  });
  mockInjected.length = 0;
  fireEvent.changeText(input, `${" ".repeat(16)}ls`);
  fireEvent.changeText(input, `${" ".repeat(16)}l`);
  expect(mockInjected).toEqual([
    expect.stringContaining('.text("ls",'),
    expect.stringContaining('.key({"key":"Backspace"'),
  ]);
  // Emptied input: the key press alone reports the backspace.
  fireEvent.changeText(input, "");
  mockInjected.length = 0;
  fireEvent(input, "keyPress", { nativeEvent: { key: "Backspace" } });
  expect(mockInjected).toEqual([expect.stringContaining('"Backspace"')]);
});

it("is read-only while Computer Use drives the desktop until control is taken", async () => {
  const fake = await renderConnected();
  act(() =>
    fake.emit("harness.subtask_started", {
      workspace_id: "ws",
      subtask_id: "s",
      child_session_id: "c",
      agent: "ComputerUse",
    }),
  );
  expect(screen.getByTestId("desktop-computer-use")).toBeTruthy();
  expect(screen.queryByTestId("desktop-show-keyboard")).toBeNull();
  await act(async () => {
    fireEvent.press(screen.getByTestId("desktop-take-control"));
  });
  expect(fake.desktop.takeControl).toHaveBeenCalledWith("ws");
  expect(screen.queryByTestId("desktop-computer-use")).toBeNull();
});

it("pastes the phone clipboard and stops the desktop from the menu", async () => {
  const fake = await renderConnected();
  fireEvent.press(screen.getByTestId("desktop-menu"));
  mockInjected.length = 0;
  await act(async () => {
    fireEvent.press(screen.getByLabelText("Paste from clipboard"));
  });
  expect(fake.desktop.writeClipboard).toHaveBeenCalledWith("ws", "from phone");
  expect(mockInjected).toEqual([
    expect.stringContaining(
      '.key({"key":"v","code":"KeyV","keyCode":86},{"ctrl":true',
    ),
  ]);

  fireEvent.press(screen.getByTestId("desktop-menu"));
  await act(async () => {
    fireEvent.press(screen.getByLabelText("Stop desktop"));
  });
  await flush();
  expect(fake.desktop.stop).toHaveBeenCalledWith("ws", {
    viewer_client_id: "11111111-2222-4333-8444-555555555555",
    intent_revision: 1,
  });
  expect(screen.getByTestId("desktop-status").props.children).toBe("Stopped");
  expect(screen.getByTestId("desktop-stopped")).toBeTruthy();
});

it("runs the screen edge to edge in portrait while the trackpad keeps its room", async () => {
  await renderConnected();
  const style = () =>
    StyleSheet.flatten(screen.getByTestId("desktop-screen").props.style);
  // 1280x720 across the full 390 points, no rounded frame.
  expect(style()).toMatchObject({ width: 390, height: 219.375 });
  expect(style().borderRadius).toBeUndefined();

  // Too little height for the full width: a framed pane fitted to it.
  fireEvent(
    screen.getByTestId("desktop-panel").children[1] as never,
    "layout",
    { nativeEvent: { layout: { width: 390, height: 360 } } },
  );
  expect(style().width).toBeLessThan(390 - 2 * 12);
  expect(style().borderRadius).toBe(12);
});
