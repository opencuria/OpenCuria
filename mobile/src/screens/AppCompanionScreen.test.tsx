import React from "react";
import { BackHandler } from "react-native";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { ThemeProvider } from "../ui/theme";
import { AppCompanionScreen } from "./AppCompanionScreen";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

const mockNative = {
  companionReady: jest.fn(),
  setCompanionTouchMode: jest.fn(),
  reloadOpenApp: jest.fn(async () => undefined),
  closeOpenAppFromCompanion: jest.fn(async () => undefined),
  menuTriggers: jest.fn(async () => ({
    shake: true,
    threeFingerPress: true,
    button: true,
  })),
  setMenuTrigger: jest.fn(async () => ({
    shake: false,
    threeFingerPress: true,
    button: true,
  })),
  openListeners: new Set<() => void>(),
};
jest.mock("../apps/companion/companionNative", () => ({
  DEFAULT_MENU_TRIGGERS: { shake: true, threeFingerPress: true, button: true },
  companionReady: () => mockNative.companionReady(),
  setCompanionTouchMode: (...args: unknown[]) =>
    mockNative.setCompanionTouchMode(...args),
  reloadOpenApp: () => mockNative.reloadOpenApp(),
  closeOpenAppFromCompanion: () => mockNative.closeOpenAppFromCompanion(),
  menuTriggers: () => mockNative.menuTriggers(),
  setMenuTrigger: (...args: [string, boolean]) =>
    mockNative.setMenuTrigger(...args),
  onCompanionOpen: (listener: () => void) => {
    mockNative.openListeners.add(listener);
    return { remove: () => mockNative.openListeners.delete(listener) };
  },
}));
jest.mock("../apps/companion/companionMemory", () => ({
  lastCompanionChat: jest.fn(async () => "chat-2"),
  rememberCompanionChat: jest.fn(async () => undefined),
}));
jest.mock("../apps/useApps", () => ({
  useApps: () => ({ rows: [] }),
}));

const mockQuiet = { elapsedMs: 0, levels: [] };
const mockMeter = {
  subscribe: () => () => undefined,
  getSnapshot: () => mockQuiet,
};
/** The one running recording (`voice/voiceRecording`), null while none. */
const mockRecording = {
  state: null as Record<string, unknown> | null,
  finish: jest.fn(),
  cancel: jest.fn(),
  setMode: jest.fn(),
};
jest.mock("../voice/voiceRecording", () => ({
  useActiveRecording: () => mockRecording.state,
  activeRecording: () =>
    mockRecording.state
      ? { id: mockRecording.state.id, meter: mockMeter }
      : null,
  finishRecording: (send: boolean) => mockRecording.finish(send),
  cancelRecording: () => mockRecording.cancel(),
  setRecordingMode: (mode: string) => mockRecording.setMode(mode),
}));
const mockAppSurface = jest.fn();
jest.mock("../apps/companion/useAppSurface", () => ({
  useAppSurface: (shown: boolean, app: unknown) => mockAppSurface(shown, app),
}));
const RECORDING = {
  id: "rec-1",
  key: "ws-1|chat-2|native",
  phase: "recording",
  mode: "use",
  surface: { kind: "app", pointable: true },
};

const conversations = [
  {
    session_id: "chat-1",
    workspace_id: "ws-1",
    workspace_name: "Demo",
    title: "Older chat",
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "m",
    unread: false,
    updated_at: "2026-11-24T10:00:00Z",
    last_message_at: "2026-11-24T10:00:00Z",
  },
  {
    session_id: "chat-2",
    workspace_id: "ws-1",
    workspace_name: "Demo",
    title: "Make the button green",
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "m",
    unread: false,
    updated_at: "2026-11-24T11:00:00Z",
    last_message_at: "2026-11-24T11:00:00Z",
  },
  {
    session_id: "chat-9",
    workspace_id: "ws-other",
    workspace_name: "Other",
    title: "Another workspace",
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "m",
    unread: false,
    updated_at: "2026-11-24T12:00:00Z",
    last_message_at: "2026-11-24T12:00:00Z",
  },
];
const mockChat = {
  client: { apps: { get: jest.fn() } },
  offline: false,
  chat: {
    activeSessionId: "chat-2",
    activeSession: { id: "chat-2", title: "Make the button green" },
  },
  conversations,
  activeConversation: conversations[1],
  workspace: { id: "ws-1", name: "Demo" },
  draftScope: { workspaceId: "ws-1", sessionId: "chat-2", harnessId: "native" },
  navigationReady: true,
  loading: false,
  ready: true,
  isTransitioning: false,
  actionBusy: false,
  voice: { record: jest.fn(async () => true) } as Record<string, unknown>,
  newChat: jest.fn(),
  openConversation: jest.fn(),
  rename: jest.fn(),
  confirmDelete: jest.fn(),
  markConversation: jest.fn(),
};
const mockOptions: unknown[] = [];
jest.mock("../state/useWorkspaceChat", () => ({
  useWorkspaceChat: (options: unknown) => {
    mockOptions.push(options);
    return mockChat;
  },
}));
jest.mock("../components/WorkspaceChatView", () => ({
  WorkspaceChatView: ({ sheets }: { sheets?: boolean }) => {
    const { Text } = require("react-native");
    return (
      <Text testID="workspace-chat">
        {sheets === false ? "chat" : "sheets"}
      </Text>
    );
  },
  WorkspaceChatSheets: () => {
    const { View } = require("react-native");
    return <View testID="workspace-chat-sheets" />;
  },
}));

const mockSession = {
  client: {
    api: { baseUrl: "https://curia.test/api/v1" },
    apps: { get: jest.fn() },
  },
  session: { organizationId: "org-a" },
  user: { id: 7 },
  offline: false,
  booting: false,
  reconnecting: false,
  retry: jest.fn(),
};
jest.mock("./SessionContext", () => ({
  useSession: () => mockSession,
}));

const ACCOUNT = "https://curia.test/api/v1|org-a|7";

function renderCompanion(
  props: Partial<React.ComponentProps<typeof AppCompanionScreen>> = {},
) {
  return render(
    <ThemeProvider>
      <AppCompanionScreen
        appId="app-1"
        appName="Todo Demo"
        versionName="1.0.2"
        workspaceId="ws-1"
        scope={ACCOUNT}
        {...props}
      />
    </ThemeProvider>,
  );
}

function rerenderCompanion(view: ReturnType<typeof renderCompanion>) {
  view.rerender(
    <ThemeProvider>
      <AppCompanionScreen
        appId="app-1"
        appName="Todo Demo"
        versionName="1.0.2"
        workspaceId="ws-1"
        scope={ACCOUNT}
      />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockOptions.length = 0;
  mockRecording.state = null;
});

describe("AppCompanionScreen", () => {
  it("opens the app's workspace chat where it was left and takes every touch", async () => {
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    expect(mockNative.companionReady).toHaveBeenCalled();
    expect(mockOptions.at(-1)).toMatchObject({
      workspaceId: "ws-1",
      initialSessionId: "chat-2",
    });
    expect(screen.getByText("Make the button green")).toBeTruthy();
    expect(screen.getByText("Todo Demo · Demo")).toBeTruthy();
    expect(mockNative.setCompanionTouchMode).toHaveBeenLastCalledWith("all");
  });

  it("goes back from the chat to the workspace's chats, newest first, and switches between them", async () => {
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    fireEvent.press(screen.getByTestId("companion-chat-back"));
    const list = await screen.findByTestId("companion-chat-list");
    expect(list).toBeTruthy();
    expect(screen.getByText("Chats")).toBeTruthy();
    expect(screen.queryByText("Another workspace")).toBeNull();
    const rows = ["chat-2", "chat-1"].map((id) => screen.getByTestId(id));
    expect(rows).toHaveLength(2);
    fireEvent.press(screen.getByTestId("chat-1"));
    expect(mockChat.openConversation).toHaveBeenCalledWith(conversations[0]);
    await waitFor(() =>
      expect(screen.queryByTestId("companion-chat-list")).toBeNull(),
    );
    expect(screen.getByTestId("workspace-chat")).toBeTruthy();
  });

  it("keeps the chat's sheets (rename, model, file) available while the chat list shows", async () => {
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    // The chat page leaves its sheets to the companion.
    expect(screen.getByTestId("workspace-chat")).toHaveTextContent("chat");
    fireEvent.press(screen.getByTestId("companion-chat-back"));
    await screen.findByTestId("companion-chat-list");
    expect(screen.queryByTestId("workspace-chat")).toBeNull();
    expect(screen.getByTestId("workspace-chat-sheets")).toBeTruthy();
  });

  it("starts a new chat from the chat list and from the chat's menu", async () => {
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    fireEvent.press(screen.getByTestId("companion-menu"));
    fireEvent.press(await screen.findByLabelText("New chat"));
    expect(mockChat.newChat).toHaveBeenCalledTimes(1);
    fireEvent.press(screen.getByTestId("companion-chat-back"));
    fireEvent.press(await screen.findByTestId("companion-chats-new"));
    expect(mockChat.newChat).toHaveBeenCalledTimes(2);
    expect(await screen.findByTestId("workspace-chat")).toBeTruthy();
  });

  it("follows the pages on Android Back: chat, chat list, then away", async () => {
    const listen = jest.spyOn(BackHandler, "addEventListener");
    const pressBack = () =>
      act(() => {
        const handler = listen.mock.calls.at(-1)![1];
        expect(handler()).toBe(true);
      });
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    pressBack();
    expect(await screen.findByTestId("companion-chat-list")).toBeTruthy();
    pressBack();
    // The sheet slides away and hands every touch back to the app.
    await waitFor(() =>
      expect(mockNative.setCompanionTouchMode).toHaveBeenLastCalledWith("none"),
    );
    expect(mockNative.closeOpenAppFromCompanion).not.toHaveBeenCalled();
    listen.mockRestore();
  });

  it("records into the chat on screen and hands every other touch to the app", async () => {
    const view = renderCompanion();
    fireEvent.press(await screen.findByTestId("companion-record"));
    await waitFor(() => expect(mockChat.voice.record).toHaveBeenCalled());
    // Recordings over an app keep its sounds playing.
    expect(mockOptions[0]).toMatchObject({ overApp: true });
    // The sheet slides away and the recording bar takes over at once.
    mockRecording.state = RECORDING;
    rerenderCompanion(view);
    expect(await screen.findByTestId("recording-bar")).toBeTruthy();
    expect(screen.getByTestId("recording-mode")).toBeTruthy();
    // The app is what the recording shows now.
    expect(mockAppSurface).toHaveBeenLastCalledWith(true, {
      app_id: "app-1",
      name: "Todo Demo",
      version: "1.0.2",
      platform: "ios",
    });
    // Only the bar's own region stays with the companion.
    const [mode, regions] = mockNative.setCompanionTouchMode.mock.lastCall!;
    expect(mode).toBe("regions");
    expect(regions).toHaveLength(1);
    expect(regions[0]).toHaveLength(4);
    // Point: the companion takes every touch and shows the laser pointer.
    fireEvent.press(
      screen.getByLabelText("Point: show something without tapping it"),
    );
    expect(mockRecording.setMode).toHaveBeenCalledWith("point");
    expect(mockNative.setCompanionTouchMode).toHaveBeenLastCalledWith("all", [
      regions[0],
    ]);
    mockRecording.state = { ...RECORDING, mode: "point" };
    rerenderCompanion(view);
    expect(screen.getByTestId("companion-pointer-overlay")).toBeTruthy();
    // Done brings the chat back, where the text arrives.
    fireEvent.press(screen.getByTestId("recording-done"));
    expect(mockRecording.finish).toHaveBeenCalledWith(false);
  });

  it("shows the bar over the chat list, where the composer is out of sight", async () => {
    mockRecording.state = RECORDING;
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    // In the chat the composer shows the recording; no bar.
    expect(screen.queryByTestId("recording-bar")).toBeNull();
    // The sheet covers the app: the app is not what the recording shows.
    expect(mockAppSurface).toHaveBeenLastCalledWith(false, expect.anything());
    fireEvent.press(screen.getByTestId("companion-chat-back"));
    expect(await screen.findByTestId("recording-bar")).toBeTruthy();
    // Nothing to point at in the chat list.
    expect(screen.queryByTestId("recording-mode")).toBeNull();
    // Drawn after the sheet, so its scrim never covers the bar.
    const order: string[] = [];
    const walk = (node: unknown): void => {
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node)) return node.forEach(walk);
      const element = node as {
        props?: { testID?: string };
        children?: unknown;
      };
      if (element.props?.testID) order.push(element.props.testID);
      walk(element.children);
    };
    walk(screen.toJSON());
    expect(order.indexOf("recording-bar")).toBeGreaterThan(
      order.indexOf("companion-sheet"),
    );
    fireEvent.press(screen.getByTestId("recording-cancel"));
    expect(mockRecording.cancel).toHaveBeenCalled();
  });

  it("offers Reload and Close app and keeps the last way to open it on", async () => {
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    fireEvent.press(screen.getByTestId("companion-menu"));
    fireEvent.press(await screen.findByLabelText("Open by shaking"));
    await waitFor(() =>
      expect(mockNative.setMenuTrigger).toHaveBeenCalledWith("shake", false),
    );
    fireEvent.press(screen.getByTestId("companion-menu"));
    fireEvent.press(await screen.findByLabelText("Reload app"));
    expect(mockNative.reloadOpenApp).toHaveBeenCalled();
    fireEvent.press(screen.getByTestId("companion-menu"));
    fireEvent.press(await screen.findByLabelText("Close app"));
    expect(mockNative.closeOpenAppFromCompanion).toHaveBeenCalled();
  });

  it("explains instead of chatting when the app belongs to another account", async () => {
    renderCompanion({ scope: "https://curia.test/api/v1|org-b|9" });
    expect(
      await screen.findByText(
        "This app was installed with another OpenCuria account. Switch to that account to chat about it.",
      ),
    ).toBeTruthy();
    expect(screen.queryByTestId("workspace-chat")).toBeNull();
    expect(mockOptions).toHaveLength(0);
  });

  it("shows again when the app menu is asked for", async () => {
    renderCompanion();
    await screen.findByTestId("workspace-chat");
    act(() => {
      for (const listener of mockNative.openListeners) listener();
    });
    expect(mockNative.setCompanionTouchMode).toHaveBeenLastCalledWith("all");
  });
});
