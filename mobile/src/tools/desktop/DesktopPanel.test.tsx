import React from "react";
import { Dimensions, StyleSheet } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { ThemeProvider } from "../../ui/theme";
import { currentToast, dismissToast } from "../../ui/toast";
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
let mockPageLoads = 0;
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
        React.useEffect(() => {
          mockPageLoads += 1;
        }, []);
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
      async (
        _workspaceId: string,
        proxy: string,
        intent: { intent_revision: number },
      ) => `https://curia.test${proxy}?intent_revision=${intent.intent_revision}`,
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

/** The panel's area below its status bar, as laid out natively. */
function layoutBody(width: number, height: number) {
  fireEvent(screen.getByTestId("desktop-body"), "layout", {
    nativeEvent: { layout: { width, height } },
  });
}

const initialWindow = Dimensions.get("window");
const initialScreen = Dimensions.get("screen");
/** Turn the device: the window takes this size. */
function setWindow(width: number, height: number) {
  act(() => Dimensions.set({ window: { ...initialWindow, width, height } }));
}

async function renderConnected({
  area = { width: 390, height: 700 },
  insets = { top: 0, left: 0, right: 0, bottom: 0 },
  onFullscreenChange,
}: {
  area?: { width: number; height: number };
  insets?: { top: number; left: number; right: number; bottom: number };
  onFullscreenChange?: (fullscreen: boolean) => void;
} = {}) {
  const fake = fakeClient();
  render(
    <SafeAreaProvider
      initialMetrics={{ frame: { x: 0, y: 0, ...area }, insets }}
    >
      <ThemeProvider>
        <DesktopPanel
          client={fake.client}
          workspaceId="ws"
          workspaceName="demo"
          active
          media={null}
          bottomInset={0}
          onFullscreenChange={onFullscreenChange}
        />
      </ThemeProvider>
    </SafeAreaProvider>,
  );
  layoutBody(area.width, area.height);
  await flush();
  act(() => mockPageMessage?.('{"type":"state","state":"connected"}'));
  return fake;
}

const screenStyle = () =>
  StyleSheet.flatten(screen.getByTestId("desktop-screen").props.style);

beforeEach(() => {
  // Lease renewals and release grace periods must not outlive a test.
  jest.useFakeTimers({
    doNotFake: ["queueMicrotask", "nextTick", "setImmediate"],
  });
  mockInjected.length = 0;
  mockPageMessage = null;
  mockPageLoads = 0;
});

afterEach(() => {
  cleanup();
  jest.clearAllTimers();
  jest.useRealTimers();
  Dimensions.set({ window: initialWindow, screen: initialScreen });
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
  // The viewer page opens with a ticket issued for this workspace.
  expect(fake.desktop.viewerUrl).toHaveBeenCalledWith(
    "ws",
    "/ws/desktop/ws/",
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

function startComputerUse(fake: ReturnType<typeof fakeClient>) {
  act(() =>
    fake.emit("harness.subtask_started", {
      workspace_id: "ws",
      subtask_id: "s",
      child_session_id: "c",
      agent: "ComputerUse",
    }),
  );
}

it("leaves the screen uncovered and read-only while Computer Use drives it, with Take control in the bar", async () => {
  const fake = await renderConnected();
  startComputerUse(fake);
  expect(screen.getByTestId("desktop-status").props.children).toBe(
    "Computer Use is in control",
  );
  // Nothing is laid over the screen; the action sits in the status bar.
  const remote = within(screen.getByTestId("desktop-screen"));
  expect(remote.queryByText(/Computer Use/)).toBeNull();
  expect(remote.queryByTestId("desktop-take-control")).toBeNull();
  expect(screen.queryByTestId("desktop-show-keyboard")).toBeNull();

  await act(async () => {
    fireEvent.press(screen.getByTestId("desktop-take-control"));
  });
  expect(fake.desktop.takeControl).toHaveBeenCalledWith("ws");
  expect(screen.queryByTestId("desktop-take-control")).toBeNull();
  expect(screen.getByTestId("desktop-status").props.children).toBe("Connected");
  expect(screen.getByTestId("desktop-show-keyboard")).toBeTruthy();
});

it("keeps Take control when Computer Use cannot be stopped", async () => {
  const fake = await renderConnected();
  fake.desktop.takeControl.mockRejectedValueOnce(new Error("busy"));
  startComputerUse(fake);
  await act(async () => {
    fireEvent.press(screen.getByTestId("desktop-take-control"));
  });
  expect(currentToast()?.title).toBe("Could not take control");
  expect(screen.getByTestId("desktop-take-control")).toBeTruthy();
  expect(screen.getByTestId("desktop-status").props.children).toBe(
    "Computer Use is in control",
  );
  dismissToast();
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
  // 1280x720 across the full 390 points, no frame (the 1 point radius
  // only makes Android clip the zoomed page).
  expect(screenStyle()).toMatchObject({
    width: 390,
    height: 219.375,
    borderRadius: 1,
  });
  expect(screenStyle().borderWidth).toBeUndefined();

  // Too little height for the full width: a framed pane fitted to it.
  layoutBody(390, 360);
  expect(screenStyle().width).toBeLessThan(390 - 2 * 12);
  expect(screenStyle().borderRadius).toBe(12);
});

it("shows the screen alone, like a full-screen video, while a phone is turned sideways", async () => {
  const onFullscreenChange = jest.fn();
  await renderConnected({ onFullscreenChange });
  expect(onFullscreenChange).not.toHaveBeenCalled();

  setWindow(844, 390);
  layoutBody(844, 390);
  expect(onFullscreenChange).toHaveBeenLastCalledWith(true);
  expect(screen.queryByTestId("desktop-status")).toBeNull();
  expect(screen.queryByTestId("desktop-trackpad")).toBeNull();
  expect(screen.queryByTestId("desktop-show-keyboard")).toBeNull();
  // 1280x720 fitted to the full height, without a frame.
  expect(screenStyle()).toMatchObject({ width: 693, height: 390 });
  expect(screenStyle().borderWidth).toBeUndefined();

  setWindow(390, 844);
  layoutBody(390, 700);
  expect(onFullscreenChange).toHaveBeenLastCalledWith(false);
  expect(screen.getByTestId("desktop-status").props.children).toBe("Connected");
  expect(screen.getByTestId("desktop-trackpad")).toBeTruthy();
  expect(screen.getByTestId("desktop-show-keyboard")).toBeTruthy();
  // Turning the phone never reloads the desktop page.
  expect(mockPageLoads).toBe(1);
});

it("keeps the full-screen desktop clear of the notch", async () => {
  setWindow(844, 390);
  await renderConnected({
    area: { width: 844, height: 390 },
    insets: { top: 0, left: 100, right: 100, bottom: 21 },
  });
  expect(screenStyle()).toMatchObject({ width: 644, height: 362 });
});

it("keeps screen and trackpad side by side on a tablet in landscape", async () => {
  const onFullscreenChange = jest.fn();
  setWindow(1180, 820);
  await renderConnected({
    area: { width: 1180, height: 760 },
    onFullscreenChange,
  });
  expect(onFullscreenChange).not.toHaveBeenCalled();
  expect(screen.getByTestId("desktop-status")).toBeTruthy();
  expect(screen.getByTestId("desktop-trackpad")).toBeTruthy();
});
