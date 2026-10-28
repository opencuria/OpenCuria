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

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

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
    profileTitle: "Demo",
    onProfile: jest.fn(),
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

const search = (text: string) => {
  if (!screen.queryByLabelText("Search workspaces and conversations"))
    fireEvent.press(screen.getByRole("button", { name: "Search chats" }));
  fireEvent.changeText(
    screen.getByLabelText("Search workspaces and conversations"),
    text,
  );
};

it("shows four newest chats per workspace and paginates each independently", () => {
  const { onConversation } = setup();
  expect(screen.getByTestId("Alpha-4")).toBeTruthy();
  expect(screen.queryByTestId("Alpha-5")).toBeNull();
  expect(screen.getByTestId("Beta-4")).toBeTruthy();
  expect(screen.queryByTestId("Beta-5")).toBeNull();
  fireEvent.press(
    screen.getByRole("button", { name: "Show 4 more chats in Alpha" }),
  );
  expect(screen.getByTestId("Alpha-8")).toBeTruthy();
  expect(screen.queryByTestId("Alpha-9")).toBeNull();
  expect(screen.queryByTestId("Beta-5")).toBeNull();
  fireEvent.press(
    screen.getByRole("button", { name: "Show 1 more chat in Alpha" }),
  );
  fireEvent.press(screen.getByTestId("Alpha-9"));
  expect(onConversation).toHaveBeenCalledWith(
    expect.objectContaining({ session_id: "Alpha-9" }),
  );
});

it("retains collapse and pagination through refresh and reveals an older active chat", () => {
  const { props, update } = setup();
  fireEvent.press(
    screen.getByRole("button", { name: "Show 4 more chats in Alpha" }),
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
  expect(
    within(screen.getByTestId("inbox-Stopped-1")).getByText("Stopped"),
  ).toBeTruthy();
  expect(screen.queryByTestId("section-Stopped")).toBeNull();
  expect(screen.queryByTestId("section-Deleted")).toBeNull();
  expect(
    screen.getByRole("button", { name: "All workspaces, 2" }),
  ).toBeTruthy();
});

it("marks chats that wait for the user and announces their state", () => {
  setup({
    conversations: [
      {
        ...chats("Alpha", 1)[0]!,
        needs_attention: true,
        attention_kind: "permission",
      },
      { ...chats("Beta", 1)[0]!, status: "busy" },
    ],
  });
  expect(screen.getByTestId("inbox-Alpha-1").props.accessibilityLabel).toBe(
    "Alpha chat 1, Permission required, Alpha",
  );
  expect(screen.getByTestId("Beta-1").props.accessibilityLabel).toMatch(
    /^Beta chat 1, Working, /,
  );
  expect(screen.getByTestId("workspace-busy-Beta")).toBeTruthy();
});

it("searches across chat titles and workspace names and handles no results", () => {
  setup();
  search("Beta chat 6");
  expect(screen.getByTestId("Beta-6")).toBeTruthy();
  expect(screen.queryByTestId("section-Alpha")).toBeNull();
  search("unmatched");
  expect(screen.queryByTestId("section-Beta")).toBeNull();
  expect(screen.getByText("No matching chats")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "All workspaces, 2" }),
  ).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByTestId("section-Alpha")).toBeTruthy();
});

it("clears scoped navigation state when the organization changes", () => {
  const { update } = setup();
  fireEvent.press(
    screen.getByRole("button", { name: "Show 4 more chats in Alpha" }),
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
  expect(
    screen.getByRole("button", { name: "New chat in Alpha" }),
  ).toBeDisabled();
  fireEvent.press(
    screen.getByRole("button", { name: "Actions for Alpha chat 1" }),
  );
  expect(screen.getByRole("button", { name: "Rename" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  expect(props.onConversationAction).not.toHaveBeenCalled();
  expect(
    screen
      .getByTestId("Alpha-1")
      .props.accessibilityActions.map(({ name }: { name: string }) => name),
  ).toEqual(["read"]);
});

it("starts a chat in a workspace from its header and exposes mark-all-read only when needed", () => {
  const onMarkAllRead = jest.fn();
  const { props, update } = setup({
    conversations: chats("Alpha", 1),
    onMarkAllRead,
  });
  expect(screen.queryByRole("button", { name: "Mark all as read" })).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: "New chat in Alpha" }));
  expect(props.onWorkspace).toHaveBeenCalledWith(
    expect.objectContaining({ id: "Alpha" }),
  );
  update({ conversations: [{ ...chats("Alpha", 1)[0]!, unread: true }] });
  fireEvent.press(screen.getByRole("button", { name: "Mark all as read" }));
  expect(onMarkAllRead).toHaveBeenCalledTimes(1);
});

it("offers the account and New chat from the floating footer", () => {
  const { props, update } = setup();
  fireEvent.press(screen.getByTestId("drawer-profile"));
  expect(props.onProfile).toHaveBeenCalledTimes(1);
  fireEvent.press(screen.getByRole("button", { name: "New chat" }));
  expect(props.onCreate).toHaveBeenCalledTimes(1);
  update({ canCreate: false });
  expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();
});
