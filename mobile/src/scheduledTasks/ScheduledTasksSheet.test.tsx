import React from "react";
import { Alert, type AlertButton } from "react-native";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type {
  OpenCuriaClient,
  ScheduledTask,
  ScheduledTaskRun,
  Skill,
  Workspace,
} from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { ScheduledTasksSheet } from "./ScheduledTasksSheet";
import type { AgentCatalog } from "./scheduledTaskForm";
import { clockParts, formatRunClock } from "./scheduledTaskPresentation";
import { useScheduledTasks } from "./useScheduledTasks";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

/** Tuesday, 3 November 2026, 11:00 on the test machine's clock. */
const NOW = new Date(2026, 10, 3, 11, 0);
const at = (day: number, hour: number, minute = 0) =>
  new Date(2026, 10, day, hour, minute).toISOString();

function workspace(id: string, name: string, status = "running"): Workspace {
  return {
    id,
    name,
    status,
    runner_id: "runner",
    active_operation: null,
    runner_online: true,
    has_active_session: false,
    created_at: at(1, 9),
    updated_at: at(1, 9),
  } as Workspace;
}

const billing = workspace("ws-billing", "billing-service");
const api = workspace("ws-api", "api-gateway", "stopped");

function task(overrides: Partial<ScheduledTask>): ScheduledTask {
  return {
    id: "task",
    name: "Task",
    workspace_id: billing.id,
    prompt: "Do it",
    mode: "build",
    harness_id: "native",
    model: "sonnet",
    reasoning_effort: "high",
    skill_ids: [],
    recurrence: "weekly",
    weekdays: [0, 1, 2, 3, 4],
    cron_expression: "",
    run_date: null,
    local_time: "14:30",
    timezone_name: "UTC",
    enabled: true,
    is_running: false,
    next_run_at: at(3, 14, 30),
    created_at: at(1, 9),
    updated_at: at(1, 9),
    ...overrides,
  };
}

const deps = task({ id: "task-deps", name: "Dependency review" });
const nightly = task({
  id: "task-nightly",
  name: "Nightly test sweep",
  recurrence: "daily",
  weekdays: [],
  local_time: "02:00",
  is_running: true,
  next_run_at: at(4, 2),
});
const flaky = task({
  id: "task-flaky",
  name: "Flaky test triage",
  workspace_id: api.id,
  enabled: false,
});

function run(overrides: Partial<ScheduledTaskRun>): ScheduledTaskRun {
  return {
    id: "run",
    scheduled_for: at(2, 14, 30),
    trigger: "scheduled",
    status: "succeeded",
    session_id: "session",
    started_at: at(2, 14, 30),
    finished_at: at(2, 14, 36),
    error: "",
    reason: "",
    assistant_finish: "",
    assistant_error: "",
    ...overrides,
  };
}

const skills = [
  { id: "skill-review", name: "Code review checklist" },
] as Skill[];

const catalog: AgentCatalog = {
  engines: [
    {
      id: "native",
      name: "OpenCuria",
      modes: ["build", "plan"],
      connected: true,
      default_model: null,
      default_effort: null,
    },
  ],
  models: [
    {
      id: "sonnet",
      name: "Sonnet",
      reasoning_efforts: ["low", "high"],
      default_effort: "low",
      supports_tools: true,
      context_length: 0,
      max_output_tokens: 0,
    },
  ],
  engineModels: {},
  agentConfigs: [{ agent: "build", model: "sonnet", effort: "high" }],
  recentModelIds: [],
  error: null,
};

function mockClient(tasks = [deps, nightly, flaky]) {
  return {
    scheduledTasks: {
      list: jest.fn().mockResolvedValue(tasks),
      create: jest.fn(async (input: Record<string, unknown>) =>
        task({ ...input, id: "task-new", next_run_at: at(4, 8, 15) }),
      ),
      update: jest.fn(async (id: string, patch: Record<string, unknown>) => ({
        ...tasks.find((item) => item.id === id)!,
        ...patch,
      })),
      delete: jest.fn().mockResolvedValue(undefined),
      run: jest.fn().mockResolvedValue(run({ status: "running" })),
      runs: jest.fn().mockResolvedValue([
        run({
          id: "run-failed",
          status: "error",
          error: "Workspace did not start.",
          session_id: "session-failed",
          configuration_snapshot: { workspace_id: "ws-before" },
        }),
        run({ id: "run-ok", scheduled_for: at(1, 14, 30) }),
      ]),
    },
    realtime: {
      on: jest.fn(() => () => undefined),
      onReconnect: jest.fn(() => () => undefined),
    },
  };
}

function Harness({
  client,
  onOpenChat,
}: {
  client: ReturnType<typeof mockClient>;
  onOpenChat: (workspaceId: string, sessionId: string) => void;
}): React.JSX.Element {
  const schedules = useScheduledTasks(
    client as unknown as OpenCuriaClient,
    "server:org",
  );
  return (
    <ScheduledTasksSheet
      visible
      onClose={jest.fn()}
      client={client as unknown as OpenCuriaClient}
      schedules={schedules}
      workspaces={[billing, api]}
      skills={skills}
      catalog={catalog}
      defaultWorkspaceId={billing.id}
      onOpenChat={onOpenChat}
    />
  );
}

function renderSheet(client = mockClient()) {
  const onOpenChat = jest.fn();
  const tree = render(
    <ThemeProvider>
      <Harness client={client} onOpenChat={onOpenChat} />
    </ThemeProvider>,
  );
  return { client, tree, onOpenChat };
}

/** Press the button of the alert currently shown. */
function alertButton(alert: jest.SpyInstance, text: string): AlertButton {
  const buttons = alert.mock.calls.at(-1)?.[2] as AlertButton[];
  return buttons.find((button) => button.text === text)!;
}

beforeEach(() => {
  jest.useFakeTimers({ now: NOW, advanceTimers: true });
});
afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

it("lists an agenda led by run times and opens a task with its runs", async () => {
  const { client, onOpenChat, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running now")).toBeTruthy());
  expect(screen.getByText("Today")).toBeTruthy();
  expect(screen.getByText("Paused")).toBeTruthy();
  const time = formatRunClock(new Date(deps.next_run_at));
  const clock = clockParts(new Date(deps.next_run_at));
  fireEvent.press(
    screen.getByRole("button", {
      name: `${[clock.time, clock.period].filter(Boolean).join(" ")}, Dependency review, Weekdays · 14:30 · billing-service`,
    }),
  );

  await waitFor(() =>
    expect(client.scheduledTasks.runs).toHaveBeenCalledWith(deps.id),
  );
  expect(screen.getByText("Scheduled task")).toBeTruthy();
  await waitFor(() =>
    expect(
      screen.getByText(`Last run failed · Next run today at ${time}`),
    ).toBeTruthy(),
  );
  expect(screen.getByText("Failed")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: /^Failed, / }));
  // The chat lives in the workspace the run used, not the task's current one.
  expect(onOpenChat).toHaveBeenCalledWith("ws-before", "session-failed");
  tree.unmount();
});

it("creates a task from native schedule controls", async () => {
  const { client, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Today")).toBeTruthy());
  fireEvent.press(screen.getByTestId("new-scheduled-task"));
  expect(screen.getByTestId("scheduled-task-sheet")).toBeTruthy();
  const save = screen.getByTestId("scheduled-task-save");
  expect(save.props.accessibilityState.disabled).toBe(true);

  fireEvent.changeText(screen.getByLabelText("Name"), "Morning brief");
  fireEvent.changeText(
    screen.getByLabelText("Prompt"),
    "Summarize overnight alerts.",
  );
  fireEvent.press(screen.getByRole("radio", { name: "At set times" }));
  fireEvent.press(screen.getByRole("checkbox", { name: "Saturday" }));
  fireEvent.press(screen.getByRole("checkbox", { name: "Sunday" }));
  fireEvent.changeText(screen.getByLabelText("Time"), "08:15");
  fireEvent.press(
    screen.getByRole("switch", { name: "Code review checklist" }),
  );
  expect(screen.getByText("Weekdays")).toBeTruthy();

  await act(async () =>
    fireEvent.press(screen.getByTestId("scheduled-task-save")),
  );
  expect(client.scheduledTasks.create).toHaveBeenCalledWith(
    expect.objectContaining({
      name: "Morning brief",
      workspace_id: billing.id,
      prompt: "Summarize overnight alerts.",
      mode: "build",
      harness_id: "native",
      model: "sonnet",
      reasoning_effort: "high",
      skill_ids: ["skill-review"],
      recurrence: "weekly",
      weekdays: [0, 1, 2, 3, 4],
      local_time: "08:15",
      enabled: true,
    }),
  );
  await waitFor(() => expect(screen.getByText("Morning brief")).toBeTruthy());
  expect(screen.queryByTestId("scheduled-task-sheet")).toBeNull();
  tree.unmount();
});

it("runs, pauses and deletes a task from its row menu", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
  const { client, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Today")).toBeTruthy());

  fireEvent.press(
    screen.getByRole("button", { name: "Actions for Dependency review" }),
  );
  fireEvent.press(screen.getByRole("button", { name: "Run now" }));
  await waitFor(() =>
    expect(client.scheduledTasks.run).toHaveBeenCalledWith(deps.id),
  );

  fireEvent.press(
    screen.getByRole("button", { name: "Actions for Dependency review" }),
  );
  fireEvent.press(screen.getByRole("button", { name: "Pause" }));
  await waitFor(() =>
    expect(client.scheduledTasks.update).toHaveBeenCalledWith(deps.id, {
      enabled: false,
    }),
  );
  // The paused task moves out of today's agenda.
  await waitFor(() => expect(screen.queryByText("Today")).toBeNull());

  fireEvent.press(
    screen.getByRole("button", { name: "Actions for Flaky test triage" }),
  );
  fireEvent.press(screen.getByRole("button", { name: "Delete" }));
  expect(alert.mock.calls.at(-1)?.[0]).toBe("Delete “Flaky test triage”?");
  expect(client.scheduledTasks.delete).not.toHaveBeenCalled();
  await act(async () => alertButton(alert, "Delete").onPress?.());
  expect(client.scheduledTasks.delete).toHaveBeenCalledWith(flaky.id);
  await waitFor(() =>
    expect(screen.queryByText("Flaky test triage")).toBeNull(),
  );
  tree.unmount();
});

it("saves only changed fields and asks before discarding edits", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
  const { client, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Today")).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: /Dependency review, / }));
  await waitFor(() => expect(screen.getByText("Scheduled task")).toBeTruthy());
  const save = () => screen.getByTestId("scheduled-task-save");
  expect(save().props.accessibilityState.disabled).toBe(true);

  fireEvent.changeText(screen.getByLabelText("Name"), "Deps");
  // Run now uses the saved settings, so it waits for the edit to be saved.
  expect(
    screen.getByTestId("task-header-run").props.accessibilityState.disabled,
  ).toBe(true);
  const close = screen.getAllByRole("button", { name: "Close" }).at(-1)!;
  fireEvent.press(close);
  expect(alert.mock.calls.at(-1)?.[0]).toBe("Discard changes?");

  await act(async () => fireEvent.press(save()));
  expect(client.scheduledTasks.update).toHaveBeenCalledWith(deps.id, {
    name: "Deps",
  });
  await waitFor(() => expect(screen.queryByText("Scheduled task")).toBeNull());
  expect(screen.getByText("Deps")).toBeTruthy();
  tree.unmount();
});

it("explains a failed load and retries", async () => {
  const client = mockClient();
  client.scheduledTasks.list
    .mockRejectedValueOnce(new Error("Server unavailable"))
    .mockResolvedValueOnce([deps]);
  const { tree } = renderSheet(client);
  await waitFor(() =>
    expect(
      screen.getByText("Could not load scheduled tasks: Server unavailable"),
    ).toBeTruthy(),
  );
  await act(async () =>
    fireEvent.press(screen.getByRole("button", { name: "Retry" })),
  );
  await waitFor(() =>
    expect(screen.getByText("Dependency review")).toBeTruthy(),
  );
  tree.unmount();
});
