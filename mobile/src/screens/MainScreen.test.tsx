import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react-native";
import type {
  HarnessConversation,
  HarnessSession,
  ProviderModel,
  Workspace,
} from "@opencuria/client";
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
const mockConsumeTarget = jest.fn((target?: NotificationState["target"]) => {
  if (target && mockNotificationState.target !== target) return;
  mockNotificationState.target = null;
});
const mockNotificationController = {
  consumeTarget: mockConsumeTarget,
  live: { available: false },
};
jest.mock(
  "remend",
  () => ({ __esModule: true, default: (markdown: string) => markdown }),
  { virtual: true },
);

jest.mock("../notifications/NotificationContext", () => ({
  useNotifications: () => ({
    controller: mockNotificationController,
    state: mockNotificationState,
  }),
}));
jest.mock("../notifications/native", () => ({
  setViewedNotificationSession: jest.fn(),
}));
const mockSavedAccount = {
  loadOverview: jest.fn(),
  saveOverview: jest.fn(),
  chats: jest.fn(),
  loadChat: jest.fn(),
  markOpened: jest.fn(),
  saveChat: jest.fn(),
  saveMedia: jest.fn(),
  media: jest.fn(),
};
jest.mock("../offline/offlineStore", () => ({
  offlineStore: () => ({ account: () => mockSavedAccount }),
}));
jest.mock("../offline/useSavedChatCleanup", () => ({
  useSavedChatCleanup: jest.fn(),
}));
/** The props the transcript got last, to call its handlers directly. */
const mockTranscript: {
  props?: {
    resolveMedia?: (path: string) => Promise<unknown>;
    onFork?: unknown;
    onEdit?: unknown;
  };
} = {};

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
    ChatComposer: function MockComposer(props: {
      draft: string;
      settings?: { model?: string; effort?: string };
      workspace?: {
        name: string;
        actions: Array<{ id: string; title: string; selected?: boolean }>;
        onSelect: (id: string) => void;
      };
      placeholder?: string;
      disabled?: boolean;
      canSend?: boolean;
      onSettings: () => void;
      onDraftChange: (value: string) => void;
      onSend: (value: {
        text: string;
        skillIds: string[];
        attachments: [];
      }) => Promise<void>;
    }) {
      const [menuOpen, setMenuOpen] = ReactRuntime.useState(false);
      return ReactRuntime.createElement(
        View,
        null,
        props.workspace
          ? ReactRuntime.createElement(
              View,
              { testID: "composer-workspace-menu" },
              ReactRuntime.createElement(
                Pressable,
                {
                  testID: "composer-workspace",
                  onPress: () => setMenuOpen(true),
                },
                ReactRuntime.createElement(Text, null, props.workspace.name),
              ),
              ...(menuOpen
                ? props.workspace.actions.map((action) =>
                    ReactRuntime.createElement(Pressable, {
                      key: action.id,
                      accessibilityRole: "button",
                      accessibilityLabel: action.title,
                      accessibilityState: {
                        selected: Boolean(action.selected),
                      },
                      onPress: () => {
                        setMenuOpen(false);
                        props.workspace?.onSelect(action.id);
                      },
                    }),
                  )
                : []),
            )
          : null,
        ReactRuntime.createElement(
          Text,
          { testID: "composer-draft" },
          props.draft,
        ),
        ReactRuntime.createElement(
          Pressable,
          { testID: "test-settings", onPress: props.onSettings },
          ReactRuntime.createElement(Text, null, "Settings"),
        ),
        ReactRuntime.createElement(
          Pressable,
          {
            testID: "test-draft",
            onPress: () => props.onDraftChange("Review open Dependabot PRs"),
          },
          ReactRuntime.createElement(Text, null, "Draft"),
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
          Text,
          { testID: "composer-send" },
          props.canSend === false ? "no-send" : "send",
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
      );
    },
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
    ChatMessages: (props: {
      loading?: boolean;
      emptyText?: string;
      resolveMedia?: (path: string) => Promise<unknown>;
      messages: Array<{ content: string }>;
      onPullUpAtEnd?: () => void;
      onOpenFile: (path: string) => void;
      onOpenAttachment: (attachment: {
        uri: string;
        filename: string;
        mime: string;
      }) => void;
    }) => {
      mockTranscript.props = props;
      return ReactRuntime.createElement(
        View,
        null,
        ReactRuntime.createElement(
          Text,
          { testID: "transcript-state" },
          props.loading
            ? "Loading chat"
            : props.messages.map((item) => item.content).join("\n") ||
                (props.emptyText ?? "Empty chat"),
        ),
        props.onPullUpAtEnd
          ? ReactRuntime.createElement(Pressable, {
              testID: "test-pull-up",
              onPress: props.onPullUpAtEnd,
            })
          : null,
        ReactRuntime.createElement(Pressable, {
          testID: "test-file-link",
          onPress: () => props.onOpenFile("/workspace/index.ts"),
        }),
        ReactRuntime.createElement(Pressable, {
          testID: "test-attachment",
          onPress: () =>
            props.onOpenAttachment({
              uri: "data:application/pdf;base64,JVBERg==",
              mime: "application/pdf",
              filename: "report.pdf",
            }),
        }),
      );
    },
  };
});
jest.mock("../tools/WorkspaceToolsSheet", () => {
  const ReactRuntime = require("react") as typeof React;
  const { Text } = require("react-native") as typeof import("react-native");
  return {
    WorkspaceToolsSheet: (props: {
      visible: boolean;
      workspaceName: string;
    }) =>
      props.visible
        ? ReactRuntime.createElement(
            Text,
            { testID: "workspace-tools-sheet" },
            props.workspaceName,
          )
        : null,
  };
});
jest.mock("../tools/git", () => ({ releaseGitControllers: jest.fn() }));
jest.mock("../tools/terminal", () => ({ releaseTerminalSessions: jest.fn() }));
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
        default_model: null,
        default_effort: null,
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
    runners: { list: jest.fn().mockResolvedValue([]) },
    plugins: { list: jest.fn().mockResolvedValue([]) },
    credentials: { list: jest.fn().mockResolvedValue([]) },
    images: {
      listCaptured: jest.fn().mockResolvedValue([]),
      listDefinitions: jest.fn().mockResolvedValue([]),
      listRunnerBuilds: jest.fn().mockResolvedValue([]),
      createWorkspace: jest.fn(),
    },
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
    scheduledTasks: {
      list: jest.fn().mockResolvedValue([]),
      runs: jest.fn().mockResolvedValue([]),
      create: jest.fn(),
    },
    files: {
      findFiles: jest.fn().mockResolvedValue({ paths: [] }),
      readFile: jest
        .fn()
        .mockResolvedValue({ content: btoa("const answer = 42;"), size: 18 }),
    },
  };
}

beforeEach(() => {
  mockNotificationState.target = null;
  jest.clearAllMocks();
  listeners.clear();
  mockTranscript.props = undefined;
  mockSavedAccount.loadOverview.mockResolvedValue(null);
  mockSavedAccount.saveOverview.mockResolvedValue(undefined);
  mockSavedAccount.chats.mockResolvedValue([]);
  mockSavedAccount.loadChat.mockResolvedValue(null);
  mockSavedAccount.markOpened.mockResolvedValue(undefined);
  mockSavedAccount.saveChat.mockResolvedValue(undefined);
  mockSavedAccount.saveMedia.mockResolvedValue(undefined);
  mockSavedAccount.media.mockResolvedValue(null);
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
      busy: false,
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
    offline: false,
    reconnecting: false,
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
  expect(screen.getByTestId("composer-workspace")).toHaveTextContent(
    runningWorkspace.name,
  );
  expect(client.harness.listEngineModels).not.toHaveBeenCalledWith("claude");
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

const busySession = {
  id: "running-chat",
  workspace_id: runningWorkspace.id,
  title: "Running chat",
  harness_id: "native",
  model: nativeModel.id,
  mode: "build",
  status: "busy",
  skill_ids: [],
} as HarnessSession;

it.each(["header", "sidebar", "workspace"])(
  "starts and sends a new chat from the %s while another chat is running",
  async (entry) => {
    const client = mockClient();
    client.workspaces.list.mockResolvedValue([
      { ...runningWorkspace, has_active_session: true },
    ]);
    mockUseSession.mockReturnValue(sessionValue(client) as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() =>
      expect(mockController.newChat).toHaveBeenCalledWith(runningWorkspace.id),
    );
    await act(async () => {
      snapshot = {
        ...snapshot,
        sessions: [busySession] as never[],
        activeSessionId: busySession.id,
        activeSession: busySession as never,
        busy: true,
      };
      listeners.forEach((listener) => listener());
    });
    mockController.newChat.mockClear();

    if (entry === "header") {
      expect(screen.getByTestId("new-chat")).toBeEnabled();
      fireEvent.press(screen.getByTestId("new-chat"));
    } else {
      fireEvent.press(screen.getByTestId("main-menu"));
      const button =
        entry === "sidebar"
          ? screen.getByTestId("drawer-new-chat")
          : screen.getByRole("button", {
              name: `New chat in ${runningWorkspace.name}`,
            });
      expect(button).toBeEnabled();
      fireEvent.press(button);
    }
    await waitFor(() =>
      expect(mockController.newChat).toHaveBeenCalledTimes(1),
    );
    expect(snapshot.activeSessionId).toBeNull();
    expect(snapshot.sessions).toEqual([busySession]);
    expect(screen.queryByTestId("drawer-new-chat")).toBeNull();
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    );
    fireEvent.press(screen.getByTestId("test-send"));
    await waitFor(() => expect(mockController.send).toHaveBeenCalled());
    expect(mockController.abort).not.toHaveBeenCalled();
    tree.unmount();
  },
);

it.each([false, true])(
  "continues an idle chat while a different chat is busy (workspace busy flag: %s)",
  async (hasActiveSession) => {
    const client = mockClient();
    client.workspaces.list.mockResolvedValue([
      { ...runningWorkspace, has_active_session: hasActiveSession },
    ]);
    mockUseSession.mockReturnValue(sessionValue(client) as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
    const idleSession = { ...busySession, id: "idle-chat", status: "idle" };
    await act(async () => {
      snapshot = {
        ...snapshot,
        sessions: [busySession, idleSession] as never[],
        activeSessionId: idleSession.id,
        activeSession: idleSession as never,
      };
      listeners.forEach((listener) => listener());
    });
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    );
    fireEvent.press(screen.getByTestId("test-send"));
    await waitFor(() => expect(mockController.send).toHaveBeenCalled());
    tree.unmount();
  },
);

it.each([
  { status: "stopped" as const },
  { runner_online: false },
  { active_operation: "capturing_image" },
  { intervention_required: true },
])("keeps an unavailable workspace blocked with %o", async (unavailable) => {
  const client = mockClient();
  client.workspaces.list.mockResolvedValue([
    { ...runningWorkspace, has_active_session: true, ...unavailable },
  ]);
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.openWorkspace).toHaveBeenCalled());
  await act(async () => {
    snapshot = { ...snapshot, workspaceId: runningWorkspace.id };
    listeners.forEach((listener) => listener());
  });
  expect(mockController.newChat).not.toHaveBeenCalled();
  expect(screen.queryByTestId("workspace-status")).toBeNull();
  expect(screen.queryByTestId("runner-offline")).toBeNull();
  expect(screen.queryByTestId("subtask-readonly")).toBeNull();
  expect(screen.getByTestId("new-chat")).toBeDisabled();
  expect(screen.getByTestId("composer-state")).toHaveTextContent(/\|disabled$/);
  expect(
    screen.queryByText(
      /wait for the run to finish before starting another chat/i,
    ),
  ).toBeNull();
  fireEvent.press(screen.getByTestId("main-menu"));
  expect(screen.getByTestId("drawer-new-chat")).toBeDisabled();
  const workspaceButton = screen.queryByRole("button", {
    name: `New chat in ${runningWorkspace.name}`,
  });
  if (workspaceButton) expect(workspaceButton).toBeDisabled();
  expect(mockController.send).not.toHaveBeenCalled();
  tree.unmount();
});

it("keeps the composer mounted through initial loading, empty and existing chats", async () => {
  const client = mockClient();
  let finishOverview!: (value: Workspace[]) => void;
  client.workspaces.list.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishOverview = resolve;
      }),
  );
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  const composer = screen.getByTestId("test-settings");
  expect(screen.getByTestId("composer-state")).toHaveTextContent(
    "Ask OpenCuria|disabled",
  );

  await act(async () => finishOverview([runningWorkspace]));
  await waitFor(() =>
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    ),
  );
  expect(screen.getByTestId("test-settings")).toBe(composer);
  act(() => {
    snapshot = { ...snapshot, activeSessionId: "existing-chat" };
    listeners.forEach((listener) => listener());
  });
  expect(screen.getByTestId("test-settings")).toBe(composer);
  expect(screen.queryByTestId("composer-workspace")).toBeNull();
  fireEvent.press(screen.getByTestId("new-chat"));
  await waitFor(() => expect(snapshot.activeSessionId).toBeNull());
  expect(screen.getByTestId("test-settings")).toBe(composer);
  tree.unmount();
});

it.each(["success", "failure"])(
  "settles initial loading when a background refresh overtakes it with %s",
  async (outcome) => {
    const client = mockClient();
    let finishInitial!: (value: Workspace[]) => void;
    client.workspaces.list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishInitial = resolve;
        }),
    );
    const value = sessionValue(client);
    mockUseSession.mockReturnValue(value as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    expect(screen.getByTestId("transcript-state")).toHaveTextContent(
      "Loading chat",
    );
    if (outcome === "failure")
      client.workspaces.list.mockRejectedValueOnce(new Error("Offline"));

    const calls = value.registerForegroundRefresh.mock.calls;
    const foreground = calls[calls.length - 1]?.[0] as () => Promise<void>;
    await act(async () => {
      await foreground();
    });
    expect(screen.getByTestId("transcript-state")).not.toHaveTextContent(
      "Loading chat",
    );
    if (outcome === "success") {
      expect(screen.getByTestId("composer-state")).toHaveTextContent(
        "Ask OpenCuria|ready",
      );
    } else {
      expect(screen.getByText("Offline")).toBeTruthy();
      fireEvent.press(screen.getByText("Retry"));
      await waitFor(() =>
        expect(screen.getByTestId("composer-state")).toHaveTextContent(
          "Ask OpenCuria|ready",
        ),
      );
    }

    await act(async () => {
      finishInitial([]);
    });
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    );
    expect(screen.getByTestId("transcript-state")).not.toHaveTextContent(
      "Loading chat",
    );
    tree.unmount();
  },
);

it("schedules the drafted prompt in the open chat's workspace from the chat menu", async () => {
  const client = mockClient();
  client.scheduledTasks.list.mockResolvedValue([
    {
      id: "task-1",
      name: "Nightly",
      workspace_id: runningWorkspace.id,
      enabled: true,
      is_running: true,
      recurrence: "daily",
      weekdays: [],
      local_time: "02:00",
      timezone_name: "UTC",
      next_run_at: "2099-01-01T02:00:00Z",
    },
  ]);
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
  fireEvent.press(screen.getByRole("button", { name: "Open sidebar" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Scheduled tasks, 1, Running" }),
    ).toBeTruthy(),
  );

  fireEvent.press(screen.getByTestId("test-draft"));
  fireEvent.press(screen.getByTestId("chat-menu"));
  fireEvent.press(screen.getByRole("button", { name: "Schedule this prompt" }));
  expect(screen.getByTestId("scheduled-task-sheet")).toBeTruthy();
  expect(screen.getByLabelText("Prompt").props.value).toBe(
    "Review open Dependabot PRs",
  );
  const workspace = screen.getByRole("button", {
    name: runningWorkspace.name,
  });
  expect(workspace.props.accessibilityState.selected).toBe(true);
  tree.unmount();
});

it("keeps the composer mounted when switching workspaces", async () => {
  const client = mockClient();
  client.workspaces.list.mockResolvedValue([
    runningWorkspace,
    { ...runningWorkspace, id: "workspace-b", name: "Other workspace" },
  ]);
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
  const composer = screen.getByTestId("test-settings");
  fireEvent.press(screen.getByTestId("chat-menu"));
  fireEvent.press(screen.getByRole("button", { name: "Switch workspace" }));
  fireEvent.press(screen.getByRole("button", { name: /^Other workspace,/ }));
  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenLastCalledWith("workspace-b"),
  );
  expect(screen.getByTestId("test-settings")).toBe(composer);
  tree.unmount();
});

it("moves a new chat's draft and model choice to the workspace picked in the composer", async () => {
  const client = mockClient();
  client.workspaces.list.mockResolvedValue([
    runningWorkspace,
    { ...runningWorkspace, id: "workspace-b", name: "Other workspace" },
  ]);
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("composer-workspace")).toHaveTextContent(
      "Ready workspace",
    ),
  );
  const menu = () => {
    fireEvent.press(screen.getByTestId("composer-workspace"));
    return within(screen.getByTestId("composer-workspace-menu"));
  };
  expect(
    menu().getByRole("button", { name: "Ready workspace" }).props
      .accessibilityState.selected,
  ).toBe(true);
  fireEvent.press(screen.getByTestId("test-draft"));
  fireEvent.press(screen.getByTestId("test-settings"));
  fireEvent.press(screen.getByRole("radio", { name: "Low" }));
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toBe(
      "Native model|Low",
    ),
  );

  fireEvent.press(menu().getByRole("button", { name: "Other workspace" }));
  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenLastCalledWith("workspace-b"),
  );
  expect(screen.getByTestId("composer-workspace")).toHaveTextContent(
    "Other workspace",
  );
  expect(screen.getByTestId("composer-draft")).toHaveTextContent(
    "Review open Dependabot PRs",
  );
  expect(screen.getByTestId("chat-settings").props.children).toBe(
    "Native model|Low",
  );
  fireEvent.press(screen.getByTestId("test-send"));
  await waitFor(() =>
    expect(mockController.send).toHaveBeenCalledWith(
      "hello from mobile",
      expect.objectContaining({ reasoningEffort: "low" }),
    ),
  );

  // Once the chat exists, its workspace is fixed.
  act(() => {
    snapshot = { ...snapshot, activeSessionId: "new-chat" };
    listeners.forEach((listener) => listener());
  });
  expect(screen.queryByTestId("composer-workspace")).toBeNull();
  tree.unmount();
});

it.each([
  ["the current workspace", "workspace-a", false],
  ["another workspace", "workspace-b", false],
  ["the inbox", "workspace-b", true],
])(
  "opens an existing chat from %s and closes the sidebar",
  async (_, workspaceId, unread) => {
    const client = mockClient();
    client.workspaces.list.mockResolvedValue([
      runningWorkspace,
      { ...runningWorkspace, id: "workspace-b", name: "Other workspace" },
    ]);
    client.conversations.list.mockResolvedValue([
      {
        session_id: "existing-chat",
        workspace_id: workspaceId,
        workspace_name: "Workspace",
        title: "Existing chat",
        status: "idle",
        unread,
        needs_attention: false,
        updated_at: "2026-01-01T00:00:00Z",
        last_message_at: "2026-01-01T00:00:00Z",
      } as HarnessConversation,
    ]);
    mockUseSession.mockReturnValue(sessionValue(client) as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
    fireEvent.press(screen.getByTestId("main-menu"));
    fireEvent.press(
      screen.getByTestId(`${unread ? "inbox-" : ""}existing-chat`),
    );
    await waitFor(() =>
      expect(mockController.openWorkspace).toHaveBeenCalledWith(
        workspaceId,
        "existing-chat",
      ),
    );
    expect(screen.queryByTestId("existing-chat")).toBeNull();
    tree.unmount();
  },
);

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

it("opens a warm notification and consumes it only after transcript hydration", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
  fireEvent.press(screen.getByTestId("main-menu"));
  let finish!: () => void;
  mockController.openWorkspace.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  );
  const target = {
    deviceId: "device",
    organizationId: "org-a",
    workspaceId: runningWorkspace.id,
    sessionId: "notified-session",
  };
  mockNotificationState.target = target;
  tree.rerender(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );

  await waitFor(() =>
    expect(mockController.openWorkspace).toHaveBeenCalledWith(
      runningWorkspace.id,
      target.sessionId,
    ),
  );
  expect(screen.queryByTestId("workspace-sidebar")).toBeNull();
  expect(mockConsumeTarget).not.toHaveBeenCalled();
  await act(async () => {
    finish();
  });
  await waitFor(() => expect(mockConsumeTarget).toHaveBeenCalledWith(target));
  tree.unmount();
});

it("retries a notification on foreground after opening its transcript failed", async () => {
  const client = mockClient();
  const value = sessionValue(client);
  mockController.openWorkspace.mockRejectedValueOnce(new Error("Offline"));
  mockUseSession.mockReturnValue(value as never);
  const target = {
    deviceId: "device",
    organizationId: "org-a",
    workspaceId: runningWorkspace.id,
    sessionId: "notified-session",
  };
  mockNotificationState.target = target;
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(mockController.openWorkspace).toHaveBeenCalledTimes(1),
  );
  await waitFor(() => expect(screen.getByText("Offline")).toBeTruthy());
  expect(mockConsumeTarget).not.toHaveBeenCalled();
  const foreground = value.registerForegroundRefresh.mock.calls[0]?.[0] as
    | (() => Promise<void>)
    | undefined;
  await act(async () => {
    await foreground?.();
  });
  await waitFor(() =>
    expect(mockController.openWorkspace).toHaveBeenCalledTimes(2),
  );
  await waitFor(() => expect(mockConsumeTarget).toHaveBeenCalledWith(target));
  tree.unmount();
});

it("ignores notification targets outside the selected organization", async () => {
  const client = mockClient();
  mockNotificationState.target = {
    deviceId: "device",
    organizationId: "other-org",
    workspaceId: runningWorkspace.id,
    sessionId: "notified-session",
  };
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
  expect(client.workspaces.get).not.toHaveBeenCalled();
  expect(mockController.openWorkspace).not.toHaveBeenCalled();
  expect(mockConsumeTarget).not.toHaveBeenCalled();
  tree.unmount();
});

it.each(["retry", "foreground"])(
  "retries failed initial chat navigation through %s",
  async (trigger) => {
    const client = mockClient();
    const value = sessionValue(client);
    mockController.newChat.mockRejectedValueOnce(new Error("Opening failed"));
    mockUseSession.mockReturnValue(value as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() =>
      expect(screen.getByText("Opening failed")).toBeTruthy(),
    );

    if (trigger === "retry") {
      fireEvent.press(screen.getByText("Retry"));
    } else {
      const calls = value.registerForegroundRefresh.mock.calls;
      const foreground = calls[calls.length - 1]?.[0] as
        | (() => Promise<void>)
        | undefined;
      await act(async () => {
        await foreground?.();
      });
    }
    await waitFor(() =>
      expect(mockController.newChat).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(screen.getByTestId("composer-state")).toHaveTextContent(
        "Ask OpenCuria|ready",
      ),
    );
    expect(screen.queryByText("Opening failed")).toBeNull();
    tree.unmount();
  },
);

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
  expect(screen.queryByTestId("catalog-warning")).toBeNull();
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
      default_model: null,
      default_effort: null,
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
  const sheet = within(screen.getByTestId("model-sheet"));
  fireEvent.press(sheet.getByLabelText("Engine: OpenCuria"));
  fireEvent.press(sheet.getByRole("button", { name: "Codex" }));
  await waitFor(() =>
    expect(screen.getByTestId("chat-settings").props.children).toBe(
      "Codex Sol|Low",
    ),
  );
  fireEvent.press(screen.getByRole("radio", { name: "Ultra" }));
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
    {
      id: "codex",
      name: "Codex",
      connected: true,
      modes: ["build", "plan"],
      default_model: codexModel.id,
      default_effort: "low",
    },
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
  expect(sheet.queryByTestId("model-sheet-engine")).toBeNull();
  expect(sheet.getByRole("radio", { name: "Plan" })).toBeTruthy();
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
  expect(sheet.getByRole("radio", { name: "Medium" })).toBeTruthy();
  // Only OpenCuria is available, so there is no engine to switch to.
  expect(sheet.queryByTestId("model-sheet-engine")).toBeNull();

  fireEvent.press(sheet.getByRole("radio", { name: "Plan" }));
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

it("opens Git, Desktop, Terminal and Files by pulling up at the end or from the header button", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  const tree = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() => expect(mockController.newChat).toHaveBeenCalled());
  expect(screen.queryByTestId("workspace-tools-sheet")).toBeNull();

  fireEvent.press(screen.getByTestId("test-pull-up"));
  expect(screen.getByTestId("workspace-tools-sheet")).toBeTruthy();

  tree.unmount();
  const reopened = render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    ),
  );
  fireEvent.press(screen.getByTestId("workspace-tools"));
  expect(screen.getByTestId("workspace-tools-sheet")).toBeTruthy();
  reopened.unmount();
});

it("hides the previous chat during navigation and preserves the new transcript while it loads", async () => {
  const client = mockClient();
  client.conversations.list.mockResolvedValue([
    {
      session_id: "next-chat",
      workspace_id: runningWorkspace.id,
      workspace_name: runningWorkspace.name,
      title: "Next chat",
      status: "idle",
      unread: false,
      needs_attention: false,
      updated_at: "2026-01-01T00:00:00Z",
      last_message_at: "2026-01-01T00:00:00Z",
    } as HarnessConversation,
  ]);
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
  act(() => {
    snapshot = {
      ...snapshot,
      activeSessionId: "old-chat",
      messages: [{ content: "Previous reply" }] as never[],
    };
    listeners.forEach((listener) => listener());
  });
  expect(screen.getByTestId("transcript-state")).toHaveTextContent(
    "Previous reply",
  );

  let finishNavigation!: () => void;
  mockController.openWorkspace.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        finishNavigation = resolve;
      }),
  );
  fireEvent.press(screen.getByTestId("main-menu"));
  fireEvent.press(screen.getByTestId("next-chat"));
  const transcript = screen.getByTestId("transcript-state");
  expect(transcript).toHaveTextContent("Loading chat");
  expect(screen.queryByText("Previous reply")).toBeNull();
  act(() => {
    snapshot = {
      ...snapshot,
      activeSessionId: "next-chat",
      messages: [],
      loading: true,
    };
    listeners.forEach((listener) => listener());
  });
  expect(screen.getByTestId("transcript-state")).toBe(transcript);
  await act(async () => {
    snapshot = {
      ...snapshot,
      messages: [{ content: "Next reply" }] as never[],
      loading: false,
    };
    listeners.forEach((listener) => listener());
    finishNavigation();
  });
  expect(screen.getByTestId("transcript-state")).toBe(transcript);
  expect(transcript).toHaveTextContent("Next reply");
  tree.unmount();
});

it("opens chat file links in the explorer viewer and closes back to chat", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenCalledWith(runningWorkspace.id),
  );
  fireEvent.press(screen.getByTestId("test-file-link"));
  await screen.findByTestId("file-source");
  expect(screen.getByTestId("file-viewer")).toBeTruthy();
  expect(screen.queryByTestId("media-viewer-sheet")).toBeNull();
  expect(client.files.readFile).toHaveBeenCalledWith(
    runningWorkspace.id,
    "/workspace/index.ts",
    1024 * 1024,
  );
  fireEvent(screen.getByTestId("file-viewer"), "accessibilityEscape");
  expect(screen.queryByTestId("file-viewer")).toBeNull();
  expect(screen.getByTestId("transcript-state")).toBeTruthy();
});

it("opens tool attachments in the shared file viewer", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenCalledWith(runningWorkspace.id),
  );
  fireEvent.press(screen.getByTestId("test-attachment"));
  await screen.findByTestId("file-viewer");
  expect(
    screen.getByText("report.pdf", { includeHiddenElements: true }),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "Share or open" })).toBeTruthy();
  expect(screen.queryByTestId("media-viewer-sheet")).toBeNull();
  expect(client.files.readFile).not.toHaveBeenCalled();
});

it("closes the file viewer when navigating to a different chat", async () => {
  const client = mockClient();
  mockUseSession.mockReturnValue(sessionValue(client) as never);
  render(
    <ThemeProvider>
      <MainScreen />
    </ThemeProvider>,
  );
  await waitFor(() =>
    expect(mockController.newChat).toHaveBeenCalledWith(runningWorkspace.id),
  );
  fireEvent.press(screen.getByTestId("test-file-link"));
  await screen.findByTestId("file-source");
  await act(async () => {
    snapshot = { ...snapshot, activeSessionId: "another-chat" };
    listeners.forEach((listener) => listener());
  });
  expect(screen.queryByTestId("file-viewer")).toBeNull();
});

describe("offline", () => {
  const savedRow = {
    session_id: "saved-chat",
    workspace_id: runningWorkspace.id,
    workspace_name: runningWorkspace.name,
    title: "Saved chat",
    status: "busy",
    mode: "build",
    agent_name: "build",
    model: "m",
    unread: true,
    updated_at: "2026-11-01T00:00:00Z",
    last_message_at: "2026-11-01T00:00:00Z",
  } as HarnessConversation;
  const savedSession = {
    id: "saved-chat",
    workspace_id: runningWorkspace.id,
    parent_id: null,
    title: "Saved chat",
    mode: "build",
    agent_name: "build",
    model: "m",
    status: "idle",
    cost: 0,
    tokens: {},
  } as HarnessSession;

  function saveChat() {
    mockSavedAccount.loadOverview.mockResolvedValue({
      workspaces: [runningWorkspace],
      conversations: [savedRow],
      savedAt: "2026-11-06T00:00:00Z",
    });
    mockSavedAccount.chats.mockResolvedValue([
      {
        sessionId: "saved-chat",
        workspaceId: runningWorkspace.id,
        conversation: savedRow,
        openedAt: "2026-11-06T00:00:00Z",
        savedAt: "2026-11-06T00:00:00Z",
      },
    ]);
    mockSavedAccount.loadChat.mockResolvedValue({
      session: savedSession,
      sessions: [savedSession],
      messages: [
        {
          id: "saved-message",
          session_id: "saved-chat",
          role: "assistant",
          content: "Saved answer",
          parts: [],
        },
      ],
      todos: [],
    });
  }

  it("opens the chat saved last and offers only what works without the server", async () => {
    const client = mockClient();
    saveChat();
    const retry = jest.fn().mockResolvedValue(undefined);
    mockUseSession.mockReturnValue({
      ...sessionValue(client),
      offline: true,
      retry,
    } as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("transcript-state")).toHaveTextContent(
        "Saved answer",
      ),
    );
    expect(mockSavedAccount.loadChat).toHaveBeenCalledWith("saved-chat");
    expect(mockSavedAccount.markOpened).toHaveBeenCalledWith("saved-chat");
    // Nothing asks the server.
    expect(client.workspaces.list).not.toHaveBeenCalled();
    expect(client.conversations.list).not.toHaveBeenCalled();
    expect(client.harness.listEngines).not.toHaveBeenCalled();
    expect(client.scheduledTasks.list).not.toHaveBeenCalled();
    expect(mockController.openWorkspace).not.toHaveBeenCalled();
    expect(mockController.newChat).not.toHaveBeenCalled();
    // A prompt can be written, not sent; forking and editing need the server.
    expect(screen.getByTestId("composer-state")).toHaveTextContent(
      "Ask OpenCuria|ready",
    );
    expect(screen.getByTestId("composer-send")).toHaveTextContent("no-send");
    expect(mockTranscript.props?.onFork).toBeUndefined();
    expect(mockTranscript.props?.onEdit).toBeUndefined();
    expect(screen.getByTestId("new-chat")).toBeDisabled();
    expect(screen.getByTestId("workspace-tools")).toBeDisabled();

    // The sidebar lists the saved chat, without live states, and the apps.
    fireEvent.press(screen.getByTestId("main-menu"));
    const drawer = within(screen.getByTestId("conversation-drawer"));
    expect(drawer.getByTestId("saved-chat")).toBeTruthy();
    expect(drawer.queryByTestId("inbox-saved-chat")).toBeNull();
    expect(drawer.queryByTestId("all-workspaces")).toBeNull();
    expect(drawer.queryByTestId("scheduled-tasks")).toBeNull();
    expect(drawer.getByTestId("apps")).toBeTruthy();

    // Pictures come from the copies saved with the chat.
    mockSavedAccount.media.mockResolvedValueOnce("file:///saved/shot.png");
    await expect(
      mockTranscript.props!.resolveMedia!("/workspace/shot.png"),
    ).resolves.toBe("file:///saved/shot.png");
    expect(mockSavedAccount.media).toHaveBeenCalledWith(
      "saved-chat",
      "/workspace/shot.png",
    );
    await expect(
      mockTranscript.props!.resolveMedia!("/workspace/missing.png"),
    ).rejects.toMatchObject({ retryable: false });

    fireEvent.press(
      within(screen.getByTestId("offline-notice")).getByRole("button", {
        name: "Retry",
      }),
    );
    expect(retry).toHaveBeenCalled();
    tree.unmount();
  });

  it("says when nothing is saved on this device", async () => {
    const client = mockClient();
    mockUseSession.mockReturnValue({
      ...sessionValue(client),
      offline: true,
    } as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("transcript-state")).toHaveTextContent(
        "No chats are saved on this device yet.",
      ),
    );
    tree.unmount();
  });

  it("saves the lists and the open chat online, and switches sources with the connection", async () => {
    const client = mockClient();
    client.conversations.list.mockResolvedValue([savedRow]);
    saveChat();
    mockUseSession.mockReturnValue(sessionValue(client) as never);
    const tree = render(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() =>
      expect(mockSavedAccount.saveOverview).toHaveBeenCalledWith(
        [runningWorkspace],
        [savedRow],
      ),
    );

    // Open the chat online: its transcript is saved shortly after.
    fireEvent.press(screen.getByTestId("main-menu"));
    fireEvent.press(
      within(screen.getByTestId("conversation-drawer")).getByTestId(
        "saved-chat",
      ),
    );
    await waitFor(() =>
      expect(mockController.openWorkspace).toHaveBeenCalledWith(
        runningWorkspace.id,
        "saved-chat",
      ),
    );
    act(() => {
      snapshot = {
        ...snapshot,
        workspaceId: runningWorkspace.id,
        activeSessionId: "saved-chat",
        activeSession: savedSession as never,
        sessions: [savedSession] as never[],
        messages: [
          {
            id: "live-message",
            session_id: "saved-chat",
            role: "assistant",
            content: "Live answer",
            parts: [],
          },
        ] as never[],
      };
      listeners.forEach((listener) => listener());
    });
    await waitFor(
      () =>
        expect(mockSavedAccount.saveChat).toHaveBeenCalledWith(
          expect.objectContaining({
            session: savedSession,
            messages: [expect.objectContaining({ content: "Live answer" })],
          }),
          savedRow,
        ),
      { timeout: 3000 },
    );
    expect(screen.getByTestId("transcript-state")).toHaveTextContent(
      "Live answer",
    );

    // The connection goes: the same chat shows from what was saved.
    mockUseSession.mockReturnValue({
      ...sessionValue(client),
      offline: true,
    } as never);
    tree.rerender(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("transcript-state")).toHaveTextContent(
        "Saved answer",
      ),
    );
    expect(screen.getByTestId("offline-notice")).toBeTruthy();
    expect(mockSavedAccount.loadChat).toHaveBeenCalledWith("saved-chat");

    // It comes back: the server's chat takes over again.
    mockController.openWorkspace.mockClear();
    mockUseSession.mockReturnValue(sessionValue(client) as never);
    tree.rerender(
      <ThemeProvider>
        <MainScreen />
      </ThemeProvider>,
    );
    await waitFor(() =>
      expect(mockController.openWorkspace).toHaveBeenCalledWith(
        runningWorkspace.id,
        "saved-chat",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("transcript-state")).toHaveTextContent(
        "Live answer",
      ),
    );
    expect(screen.queryByTestId("offline-notice")).toBeNull();
    tree.unmount();
  });
});
