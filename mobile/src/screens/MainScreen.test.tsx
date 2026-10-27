import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react-native";
import type { ProviderModel, Workspace } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { MainScreen } from "./MainScreen";
import { useSession } from "./SessionContext";
import type { NotificationState } from "../notifications/controller";

const mockNotificationState: NotificationState = {
  ready: false,
  busy: false,
  notifications: false,
  liveActivities: false,
  permissionGranted: false,
  capabilities: null,
  error: null,
  target: null,
};
const mockConsumeTarget = jest.fn(() => {
  mockNotificationState.target = null;
});
jest.mock("../notifications/NotificationContext", () => ({
  useNotifications: () => ({
    controller: {
      consumeTarget: mockConsumeTarget,
      live: { available: false },
    },
    state: mockNotificationState,
  }),
}));
jest.mock("../notifications/native", () => ({
  setViewedNotificationSession: jest.fn(),
}));

const listeners = new Set<() => void>();
let snapshot = {
  sessions: [] as never[],
  messages: [] as never[],
  todos: [] as never[],
  permissions: [] as never[],
  questions: [] as never[],
  activeSessionId: null as string | null,
  activeSession: null,
  activeSessionLineage: [] as never[],
  workspaceId: null as string | null,
  loading: false,
  error: null as string | null,
  connected: true,
  busy: false,
};
const mockController = {
  subscribe: jest.fn((listener: () => void) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }),
  getSnapshot: jest.fn(() => snapshot),
  dispose: jest.fn(),
  newChat: jest.fn(async (workspaceId: string) => {
    snapshot = {
      ...snapshot,
      workspaceId,
      activeSessionId: null,
      activeSession: null,
    };
    listeners.forEach((listener) => listener());
  }),
  openWorkspace: jest.fn().mockResolvedValue(undefined),
  openSession: jest.fn().mockResolvedValue(undefined),
  refresh: jest.fn().mockResolvedValue(undefined),
  send: jest.fn().mockResolvedValue({ mode: "build" }),
  abort: jest.fn().mockResolvedValue(null),
  edit: jest.fn().mockResolvedValue({}),
  fork: jest.fn().mockResolvedValue(null),
  requestPartDetail: jest.fn().mockResolvedValue({}),
  resolvePermission: jest.fn().mockResolvedValue(undefined),
  resolveQuestion: jest.fn().mockResolvedValue(undefined),
  rename: jest.fn().mockResolvedValue({}),
  delete: jest.fn().mockResolvedValue(undefined),
};
const mockUseSession = useSession as jest.MockedFunction<typeof useSession>;

jest.mock("expo-video", () => ({
  VideoView: () => null,
  useVideoPlayer: () => ({ play: jest.fn() }),
}));
jest.mock("react-native-safe-area-context", () => ({
  ...jest.requireActual("react-native-safe-area-context"),
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));
jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("../state/chatController", () => ({
  ChatController: jest.fn(() => mockController),
}));
jest.mock("../screens/SessionContext", () => ({
  ...jest.requireActual("../screens/SessionContext"),
  useSession: jest.fn(),
}));
jest.mock("../components", () => {
  const ReactRuntime = require("react") as typeof React;
  const { Pressable, Text, View } =
    require("react-native") as typeof import("react-native");
  return {
    ChatComposer: (props: {
      settings?: { model?: string; effort?: string };
      placeholder?: string;
      disabled?: boolean;
      onSettings: () => void;
      onSend: (value: {
        text: string;
        skillIds: string[];
        attachments: [];
      }) => Promise<void>;
    }) =>
      ReactRuntime.createElement(
        View,
        null,
        ReactRuntime.createElement(
          Pressable,
          { testID: "test-settings", onPress: props.onSettings },
          ReactRuntime.createElement(Text, null, "Settings"),
        ),
        ReactRuntime.createElement(
          Text,
          { testID: "chat-settings" },
          `${props.settings?.model ?? ""}|${props.settings?.effort ?? ""}`,
        ),
        ReactRuntime.createElement(
          Text,
          { testID: "composer-state" },
          `${props.placeholder ?? ""}|${props.disabled ? "disabled" : "ready"}`,
        ),
        ReactRuntime.createElement(
          Pressable,
          {
            accessibilityRole: "button",
            testID: "test-send",
            onPress: () => {
              void props.onSend({
                text: "hello from mobile",
                skillIds: [],
                attachments: [],
              });
            },
          },
          ReactRuntime.createElement(Text, null, "Send"),
        ),
      ),
    ChatGates: () => ReactRuntime.createElement(View),
    ChatLayout: ({
      children,
      composer,
      overlay,
    }: {
      children: React.ReactNode;
      composer?: React.ReactNode;
      overlay?: React.ReactNode;
    }) =>
      ReactRuntime.createElement(
        View,
        { testID: "chat-layout" },
        children,
        composer,
        overlay,
      ),
    ChatMessages: () => ReactRuntime.createElement(View),
  };
});
jest.mock("../platform", () => ({
  MediaCache: jest.fn().mockImplementation(() => ({
    dispose: jest.fn(),
    resolve: jest.fn(),
    openFile: jest.fn(),
    saveAttachment: jest.fn(),
  })),
  clearPickedPhotoCache: jest.fn(),
  pickDocuments: jest.fn().mockResolvedValue([]),
  pickPhoto: jest.fn().mockResolvedValue([]),
  uploadAttachments: jest.fn().mockResolvedValue([]),
}));

const runningWorkspace: Workspace = {
  id: "workspace-a",
  name: "Ready workspace",
  status: "running",
  runner_id: "runner",
  active_operation: null,
  runner_online: true,
  has_active_session: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};
const nativeModel: ProviderModel = {
  id: "native-model",
  name: "Native model",
  provider: "test",
  reasoning_efforts: ["low", "medium"],
  default_effort: "medium",
  supports_tools: true,
  context_length: 32000,
  max_output_tokens: 4000,
};

function mockClient() {
  const harness = {
    listEngines: jest.fn().mockResolvedValue([
      {
        id: "native",
        name: "OpenCuria",
        connected: true,
        modes: ["build", "plan"],
      },
    ]),
    listEngineModels: jest.fn(async (engine: string) => {
      if (engine === "native") return [nativeModel];
      throw new Error("Catalog unavailable");
    }),
    listAgentConfigs: jest.fn().mockResolvedValue([
      {
        agent: "build",
        mode: "build",
        model: "unavailable-default",
        effort: "high",
      },
    ]),
    listRecentModels: jest.fn().mockResolvedValue([]),
    saveRecentModel: jest.fn().mockResolvedValue({
      model: "native-model",
      effort: "medium",
      last_used_at: "now",
    }),
    markRead: jest.fn().mockResolvedValue(undefined),
    listSessions: jest.fn().mockResolvedValue([{ id: "notified-session" }]),
    markUnread: jest.fn().mockResolvedValue(undefined),
    deleteSession: jest.fn().mockResolvedValue(undefined),
  };
  return {
    api: { baseUrl: "https://curia.example/api/v1" },
    realtime: {
      isConnected: true,
      subscribeWorkspace: jest.fn(() => jest.fn()),
      on: jest.fn(() => jest.fn()),
      onReconnect: jest.fn(() => jest.fn()),
    },
    harness,
    workspaces: {
      list: jest.fn().mockResolvedValue([runningWorkspace]),
      get: jest.fn().mockResolvedValue(runningWorkspace),
    },
    conversations: { list: jest.fn().mockResolvedValue([]) },
    skills: { list: jest.fn().mockResolvedValue([]) },
    files: { findFiles: jest.fn().mockResolvedValue({ paths: [] }) },
  };
}

beforeEach(() => {
  mockNotificationState.target = null;
  jest.clearAllMocks();
  listeners.clear();
  snapshot = {
    sessions: [],
    messages: [],
    todos: [],
    permissions: [],
    questions: [],
    activeSessionId: null,
    activeSession: null,
    activeSessionLineage: [],
    workspaceId: null,
    loading: false,
    error: null,
    connected: true,
    busy: false,
  };
  mockController.newChat.mockImplementation(async (workspaceId: string) => {
    snapshot = {
      ...snapshot,
      workspaceId,
      activeSessionId: null,
      activeSession: null,
    };
    listeners.forEach((listener) => listener());
  });
  mockController.send.mockResolvedValue({ mode: "build" });
});

function sessionValue(client: ReturnType<typeof mockClient>) {
  return {
    booting: false,
    session: {
      serverUrl: "https://curia.example",
      accessToken: "a",
      refreshToken: "r",
      organizationId: "org-a",
    },
    client: client as never,
    user: {
      id: 1,
      email: "user@example.test",
      first_name: "User",
      last_name: "",
      organizations: [],
    },
    organizations: [],
    error: null,
    sessionExpired: false,
    login: jest.fn(),
    retry: jest.fn(),
    signOut: jest.fn(),
    selectOrganization: jest.fn(),
    clearError: jest.fn(),
    registerResource: jest.fn(() => jest.fn()),
    registerForegroundRefresh: jest.fn(() => jest.fn()),
  };
}

it("selects the initial ready workspace, creates a new chat and accepts the first send without loading Claude", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );

  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenCalledWith(runningWorkspace.id),
  );
  expect(client.harness.listEngineModels).toHaveBeenCalledWith("claude");
  expect(client.harness.listEngineModels).toHaveBeenCalledWith("native");
  expect(client.harness.listAgentConfigs).toHaveBeenCalled();
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toMatch(
      /^Native model/,
    ),
  );

  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() =>
    expect(mockController.send).toHaveBeenCalledWith(
      "hello from mobile",
      expect.objectContaining({
        harnessId: "native",
        mode: "build",
        model: "native-model",
        reasoningEffort: "medium",
        skillIds: [],
      }),
    ),
  );
  tree.unmount();
});

it("keeps the composer ready while a message is on its way", async () => {
  const client = mockClient();
  let accept: (value: { mode: string }) => void = () => undefined;
  mockController.send.mockImplementationOnce(
    () => new Promise((resolve) => (accept = resolve)),
  );
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    ),
  );

  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() => expect(mockController.send).toHaveBeenCalled());

  // No "unavailable workspace" hint while the send round trip is pending.
  expect(screen.getByTestId("composer-state")).toHaveTextContent(
    "Ask OpenCuria|ready",
  );
  await act(async () => accept({ mode: "build" }));
  tree.unmount();
});

it("opens a cold-start notification after checking workspace and session access", async () => {
  const client = mockClient();
  mockNotificationState.target = {
    deviceId: "device",
    organizationId: "org-a",
    workspaceId: runningWorkspace.id,
    sessionId: "notified-session",
  };
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(mockController.openWorkspace).toHaveBeenCalledWith(
      runningWorkspace.id,
      "notified-session",
    ),
  );
  expect(client.workspaces.get).toHaveBeenCalledWith(runningWorkspace.id);
  expect(client.harness.listSessions).toHaveBeenCalledWith(runningWorkspace.id);
  expect(mockConsumeTarget).toHaveBeenCalled();
  tree.unmount();
});

it("rejects a deleted notification target without navigating", async () => {
  const client = mockClient();
  client.workspaces.get.mockRejectedValue({ status: 404 });
  mockNotificationState.target = {
    deviceId: "device",
    organizationId: "org-a",
    workspaceId: runningWorkspace.id,
    sessionId: "deleted-session",
  };
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockConsumeTarget).toHaveBeenCalled());
  expect(mockController.openWorkspace).not.toHaveBeenCalled();
  expect(
    screen.getByText(
      "This notification's conversation is no longer available.",
    ),
  ).toBeTruthy();
  tree.unmount();
});

it("keeps an offline notification target available for a foreground retry", async () => {
  const client = mockClient();
  client.workspaces.get.mockRejectedValue(new Error("Offline"));
  mockNotificationState.target = {
    deviceId: "device",
    organizationId: "org-a",
    workspaceId: runningWorkspace.id,
    sessionId: "notified-session",
  };
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(
      screen.getByText(
        "Could not open this conversation. Check your connection and retry.",
      ),
    ).toBeTruthy(),
  );
  expect(mockConsumeTarget).not.toHaveBeenCalled();
  expect(mockController.openWorkspace).not.toHaveBeenCalled();
  tree.unmount();
});

it("keeps a ready workspace and sendable chat if the engine catalog fails", async () => {
  const client = mockClient();
  client.harness.listEngines.mockRejectedValue(new Error("Forbidden"));
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );

  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenCalledWith(runningWorkspace.id),
  );
  expect(screen.getByTestId("new-chat")).toBeEnabled();
  expect(screen.getByTestId("catalog-warning")).toBeTruthy();
  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() => expect(mockController.send).toHaveBeenCalled());
  tree.unmount();
});

const codexModel: ProviderModel = {
  ...nativeModel,
  id: "chatgpt/gpt-6.1-sol",
  name: "Codex Sol",
  provider: "chatgpt",
  reasoning_efforts: ["low", "ultra"],
  default_effort: "low",
};
const routerModel: ProviderModel = {
  ...nativeModel,
  id: "openrouter/vendor/model",
  name: "Router Codex",
  provider: "openrouter",
};

it("selects Codex defaults, preserves explicit effort across catalog refresh and sends the existing new-chat flow", async () => {
  const client = mockClient();
  client.harness.listEngines.mockResolvedValue([
    {
      id: "native",
      name: "OpenCuria",
      connected: true,
      modes: ["build", "plan"],
    },
    {
      id: "codex",
      name: "Codex",
      connected: true,
      modes: ["build", "plan"],
      default_model: codexModel.id,
      default_effort: "low",
    },
  ]);
  const listEngineModels = jest
    .fn()
    .mockResolvedValue([codexModel, routerModel]);
  Object.assign(client.harness, { listEngineModels });
  const value = sessionValue(client);
  mockUseSession.mockReturnValue(value as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(listEngineModels).toHaveBeenCalledWith("codex"));
  fireEvent.press(screen.getByTestId("test-settings"));
  fireEvent.press(
    within(screen.getByTestId("model-sheet")).getByText("Codex Build"),
  );
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toBe(
      "Codex Sol|Low",
    ),
  );
  fireEvent.press(screen.getByText("Ultra"));
  const foreground = value.registerForegroundRefresh.mock.calls[0]?.[0] as
    | (() => Promise<void>)
    | undefined;
  expect(foreground).toBeDefined();
  await act(async () => {
    await foreground?.();
  });
  expect(screen.getByTestId("chat-settings").props.children).toBe(
    "Codex Sol|Ultra",
  );
  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() =>
    expect(mockController.send).toHaveBeenCalledWith(
      "hello from mobile",
      expect.objectContaining({
        harnessId: "codex",
        model: codexModel.id,
        reasoningEffort: "ultra",
      }),
    ),
  );
  tree.unmount();
});

it("resumes a Codex session with an immutable engine/provider and no native-model fallback", async () => {
  const client = mockClient();
  client.harness.listEngines.mockResolvedValue([
    { id: "codex", name: "Codex", connected: true, modes: ["build", "plan"] },
  ]);
  Object.assign(client.harness, {
    listEngineModels: jest.fn().mockResolvedValue([codexModel, routerModel]),
  });
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
  await act(async () => {
    snapshot = {
      ...snapshot,
      activeSessionId: "codex-session",
      activeSession: {
        id: "codex-session",
        harness_id: "codex",
        model: routerModel.id,
        reasoning_effort: "medium",
        mode: "build",
        skill_ids: [],
      } as never,
    };
    listeners.forEach((listener) => listener());
  });
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toBe(
      "Router Codex|Medium",
    ),
  );
  fireEvent.press(screen.getByTestId("test-settings"));
  const sheet = within(screen.getByTestId("model-sheet"));
  expect(sheet.queryByText("Codex Sol")).toBeNull();
  expect(sheet.getByText("Router Codex")).toBeTruthy();
  // An existing chat keeps its engine; only its modes remain selectable.
  expect(sheet.queryByText("OpenCuria Build")).toBeNull();
  expect(sheet.getByText("Codex Plan")).toBeTruthy();
  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() =>
    expect(mockController.send).toHaveBeenCalledWith(
      "hello from mobile",
      expect.objectContaining({ harnessId: "codex", model: routerModel.id }),
    ),
  );
  tree.unmount();
});

it("offers model, effort and agent in one sheet and applies a new agent's defaults", async () => {
  const client = mockClient();
  client.harness.listAgentConfigs.mockResolvedValue([
    { agent: "build", mode: "build", model: "native-model", effort: "low" },
    { agent: "plan", mode: "plan", model: "native-model", effort: "medium" },
  ]);
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toBe(
      "Native model|Low",
    ),
  );
  fireEvent.press(screen.getByTestId("test-settings"));
  const sheet = within(screen.getByTestId("model-sheet"));
  expect(sheet.getByText("Native model")).toBeTruthy();
  expect(sheet.getByText("Medium")).toBeTruthy();
  expect(
    sheet.getByRole("button", { name: "Claude Agent Build, Not connected" }),
  ).toBeDisabled();

  fireEvent.press(sheet.getByText("OpenCuria Plan"));
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toBe(
      "Native model|Medium",
    ),
  );
  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() =>
    expect(mockController.send).toHaveBeenCalledWith(
      "hello from mobile",
      expect.objectContaining({ mode: "plan", reasoningEffort: "medium" }),
    ),
  );
  tree.unmount();
});

it("puts workspace and chat actions in the header menu", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());

  fireEvent.press(screen.getByTestId("chat-menu"));
  // A new chat has nothing to rename or delete yet.
  expect(screen.queryByRole("button", { name: "Rename chat" })).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: "Switch workspace" }));
  expect(screen.getByTestId("workspace-picker-sheet")).toBeTruthy();

  await act(async () => {
    snapshot = {
      ...snapshot,
      activeSessionId: "session-a",
      activeSession: {
        id: "session-a",
        title: "Release notes",
        harness_id: "native",
        model: "native-model",
        reasoning_effort: "low",
        mode: "build",
        skill_ids: [],
      } as never,
    };
    listeners.forEach((listener) => listener());
  });
  fireEvent.press(screen.getByTestId("chat-menu"));
  fireEvent.press(screen.getByRole("button", { name: "Rename chat" }));
  expect(screen.getByTestId("rename-session-sheet")).toBeTruthy();
  expect(screen.getByDisplayValue("Release notes")).toBeTruthy();
  tree.unmount();
});
