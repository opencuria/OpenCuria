import React from "react";
import {
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react-native";
import type { HarnessConversation, Workspace } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { WorkspaceSidebar } from "./WorkspaceSidebar";

const workspace = (id: string, status = "running"): Workspace =>
  ({ id, name: id, status, runner_online: true }) as Workspace;
const chats = (id: string, count: number): HarnessConversation[] =>
  Array.from({ length: count }, (_, index) => ({
    session_id: `${id}-${index + 1}`,
    workspace_id: id,
    workspace_name: id,
    title: `${id} chat ${index + 1}`,
    status: "idle",
    unread: false,
    needs_attention: false,
    mode: "build",
    model: "",
    agent_name: "build",
    last_message_at: new Date(Date.UTC(2026, 0, 20 - index)).toISOString(),
    updated_at: "2026-01-20T00:00:00Z",
  }));

function setup(
  overrides: Partial<React.ComponentProps<typeof WorkspaceSidebar>> = {},
) {
  const onConversation = jest.fn();
  const onAllWorkspaces = jest.fn();
  const props = {
    workspaces: [workspace("Alpha"), workspace("Beta")],
    conversations: [...chats("Alpha", 9), ...chats("Beta", 6)],
    activeWorkspaceId: "Alpha",
    activeSessionId: null,
    scopeKey: "organization-a",
    canCreate: true,
    onCreate: jest.fn(),
    onWorkspace: jest.fn(),
    onConversation,
    onConversationAction: jest.fn(),
    onAllWorkspaces,
    ...overrides,
  };
  const tree = (values = props) => (
    <ThemeProvider>
      <WorkspaceSidebar {...values} />
    </ThemeProvider>
  );
  const result = render(tree());
  return {
    ...result,
    props,
    onConversation,
    onAllWorkspaces,
    update: (changes: Partial<typeof props>) =>
      result.rerender(tree({ ...props, ...changes })),
  };
}

it("shows four newest chats per workspace and paginates each independently", () => {
  const { onConversation } = setup();
  expect(screen.getByTestId("Alpha-4")).toBeTruthy();
  expect(screen.queryByTestId("Alpha-5")).toBeNull();
  expect(screen.getByTestId("Beta-4")).toBeTruthy();
  expect(screen.queryByTestId("Beta-5")).toBeNull();
  fireEvent.press(
    within(screen.getByTestId("section-Alpha")).getByText("Show 4 more chats"),
  );
  expect(screen.getByTestId("Alpha-8")).toBeTruthy();
  expect(screen.queryByTestId("Alpha-9")).toBeNull();
  expect(screen.queryByTestId("Beta-5")).toBeNull();
  fireEvent.press(screen.getByText("Show 1 more chat"));
  fireEvent.press(screen.getByTestId("Alpha-9"));
  expect(onConversation).toHaveBeenCalledWith(
    expect.objectContaining({ session_id: "Alpha-9" }),
  );
});

it("retains collapse and pagination through refresh and reveals an older active chat", () => {
  const { props, update } = setup();
  fireEvent.press(
    within(screen.getByTestId("section-Alpha")).getByText("Show 4 more chats"),
  );
  fireEvent.press(
    screen.getByRole("button", { name: "Collapse workspace Alpha" }),
  );
  update({
    workspaces: [...props.workspaces].reverse(),
    activeSessionId: "Alpha-9",
  });
  expect(screen.queryByTestId("Alpha-9")).toBeNull();
  fireEvent.press(
    screen.getByRole("button", { name: "Expand workspace Alpha" }),
  );
  expect(screen.getByTestId("Alpha-9").props.accessibilityState.selected).toBe(
    true,
  );
});

it("uses the web inbox and workspace eligibility rules while keeping offline history reachable", () => {
  const stopped = { ...chats("Stopped", 1)[0]!, unread: true };
  setup({
    workspaces: [
      workspace("Alpha"),
      workspace("Stopped", "stopped"),
      workspace("Deleted", "deleted"),
    ],
    conversations: [...chats("Alpha", 1), stopped, ...chats("Deleted", 1)],
  });
  expect(screen.getByTestId("inbox-Stopped-1")).toBeTruthy();
  expect(screen.queryByTestId("section-Stopped")).toBeNull();
  expect(screen.queryByTestId("section-Deleted")).toBeNull();
  expect(screen.getByText("All workspaces (2)")).toBeTruthy();
});

it("searches across chat titles and workspace names and handles no results", () => {
  setup();
  fireEvent.changeText(
    screen.getByLabelText("Search workspaces and conversations"),
    "Beta chat 6",
  );
  expect(screen.getByTestId("Beta-6")).toBeTruthy();
  expect(screen.queryByTestId("section-Alpha")).toBeNull();
  fireEvent.changeText(
    screen.getByLabelText("Search workspaces and conversations"),
    "unmatched",
  );
  expect(screen.queryByTestId("section-Beta")).toBeNull();
  expect(screen.getByText("All workspaces (2)")).toBeTruthy();
});

it("clears scoped navigation state when the organization changes", () => {
  const { update } = setup();
  fireEvent.press(
    within(screen.getByTestId("section-Alpha")).getByText("Show 4 more chats"),
  );
  fireEvent.press(
    screen.getByRole("button", { name: "Collapse workspace Beta" }),
  );
  update({ scopeKey: "organization-b" });
  expect(screen.queryByTestId("Alpha-5")).toBeNull();
  expect(screen.getByTestId("Beta-4")).toBeTruthy();
});

it("keeps history readable while workspace operations disable its mutation menu", () => {
  const { props } = setup({
    workspaces: [
      { ...workspace("Alpha"), active_operation: "capturing_image" },
    ],
    conversations: chats("Alpha", 1),
  });
  fireEvent.press(screen.getByTestId("Alpha-1"));
  expect(props.onConversation).toHaveBeenCalled();
  fireEvent.press(
    screen.getByRole("button", { name: "Actions for Alpha chat 1" }),
  );
  expect(screen.getByRole("button", { name: "Rename" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  expect(props.onConversationAction).not.toHaveBeenCalled();
});

it("opens the workspace from its group header and exposes mark-all-read only when needed", () => {
  const onMarkAllRead = jest.fn();
  const { props } = setup({
    conversations: [{ ...chats("Alpha", 1)[0]!, unread: true }],
    onMarkAllRead,
  });
  fireEvent.press(screen.getByRole("button", { name: "Open workspace Alpha" }));
  expect(props.onWorkspace).toHaveBeenCalledWith(
    expect.objectContaining({ id: "Alpha" }),
  );
  fireEvent.press(screen.getByText("Mark all as read"));
  expect(onMarkAllRead).toHaveBeenCalledTimes(1);
});
