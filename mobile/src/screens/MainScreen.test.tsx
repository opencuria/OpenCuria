import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type { ProviderModel, Workspace } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { MainScreen } from "./MainScreen";
import { useSession } from "./SessionContext";

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
          Text,
          { testID: "chat-settings" },
          `${props.settings?.model ?? ""}|${props.settings?.effort ?? ""}`,
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
    ChatMessages: () => ReactRuntime.createElement(View),
  };
});
jest.mock("../platform", () => ({
  MediaCache: jest
    .fn()
    .mockImplementation(() => ({
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
  runtime_type: "docker",
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
    listEngines: jest
      .fn()
      .mockResolvedValue([
        {
          id: "native",
          name: "OpenCuria",
          connected: true,
          modes: ["build", "plan"],
        },
      ]),
    listProviderModels: jest.fn().mockResolvedValue([nativeModel]),
    listClaudeModels: jest
      .fn()
      .mockRejectedValue(new Error("Claude catalog unavailable")),
    listAgentConfigs: jest
      .fn()
      .mockResolvedValue([
        {
          agent: "build",
          mode: "build",
          model: "unavailable-default",
          effort: "high",
        },
      ]),
    listRecentModels: jest.fn().mockResolvedValue([]),
    saveRecentModel: jest
      .fn()
      .mockResolvedValue({
        model: "native-model",
        effort: "medium",
        last_used_at: "now",
      }),
    markRead: jest.fn().mockResolvedValue(undefined),
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
    workspaces: { list: jest.fn().mockResolvedValue([runningWorkspace]) },
    conversations: { list: jest.fn().mockResolvedValue([]) },
    skills: { list: jest.fn().mockResolvedValue([]) },
    files: { findFiles: jest.fn().mockResolvedValue({ paths: [] }) },
  };
}

beforeEach(() => {
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
  expect(client.harness.listClaudeModels).toHaveBeenCalled();
  expect(client.harness.listProviderModels).toHaveBeenCalled();
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

it("keeps a ready workspace and sendable chat if the engine catalog fails", async () => {
  const client = mockClient();
  client.harness.listEngines.mockRejectedValue(new Error("Forbidden"));
  client.harness.listClaudeModels.mockRejectedValue(new Error("Forbidden"));
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
