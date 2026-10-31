import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { ChatMessages, UserMessageContent } from "./ChatMessages";
import { ThemeProvider } from "../ui/theme";
import type {
  HarnessMessage,
  HarnessPart,
  HarnessTodo,
} from "@opencuria/chat-core/harness";

jest.mock("lucide-react-native", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const icon = (props: Record<string, unknown>) =>
    React.createElement(ReactNative.View, props);
  return new Proxy({}, { get: () => icon });
});
jest.mock("@legendapp/list/keyboard", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const MockList = ({
    data,
    renderItem,
    ListHeaderComponent,
    onViewableItemsChanged,
    ref: listRef,
    ...props
  }: any) => {
    React.useEffect(() => {
      onViewableItemsChanged?.({
        viewableItems: data.map((item: any) => ({ item, isViewable: true })),
      });
    }, [data, onViewableItemsChanged]);
    React.useImperativeHandle(listRef, () => ({
      scrollToEnd: mockScrollToEnd,
      getState: () => ({
        isNearEnd: mockNearEnd.current,
        listen: (type: string, callback: (value: boolean) => void) => {
          if (type !== "isNearEnd") return () => undefined;
          mockNearEndListeners.add(callback);
          return () => mockNearEndListeners.delete(callback);
        },
      }),
    }));
    return React.createElement(
      ReactNative.ScrollView,
      props,
      ListHeaderComponent,
      ...data.map((item: any, index: number) =>
        React.createElement(
          React.Fragment,
          { key: item.id },
          renderItem({ item, index }),
        ),
      ),
    );
  };
  return { KeyboardAwareLegendList: MockList };
});
const mockScrollToEnd = jest.fn().mockResolvedValue(undefined);
const mockNearEnd = { current: true };
const mockNearEndListeners = new Set<(nearEnd: boolean) => void>();
jest.mock("react-native-enriched-markdown", () => ({
  EnrichedMarkdownText: ({ markdown }: { markdown: string }) => {
    const React = require("react");
    const ReactNative = require("react-native");
    return React.createElement(ReactNative.Text, null, markdown);
  },
}));
jest.mock("expo-image", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  return {
    Image: (props: Record<string, unknown>) =>
      React.createElement(ReactNative.Image, props),
  };
});
jest.mock(
  "remend",
  () => ({ __esModule: true, default: (markdown: string) => markdown }),
  { virtual: true },
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

const message = (parts: HarnessPart[]): HarnessMessage => ({
  id: "m1",
  session_id: "s1",
  role: "assistant",
  content: "",
  parts,
});
const part = (
  id: string,
  type: HarnessPart["type"],
  output: string,
  position: number,
): HarnessPart => ({
  id,
  session_id: "s1",
  type,
  title: "",
  state: "completed",
  output,
  position,
});

beforeEach(() => {
  mockScrollToEnd.mockClear();
  mockNearEnd.current = true;
  mockNearEndListeners.clear();
});

describe("ChatMessages", () => {
  it("does not render or allow copying assistant aggregate content", () => {
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[{ ...message([]), content: "Unused aggregate" }]}
        />
      </ThemeProvider>,
    );
    expect(screen.queryByText("Unused aggregate")).toBeNull();
    expect(screen.getByRole("button", { name: "Copy message" })).toBeDisabled();
  });

  it("keeps a live reply without a server position after the persisted turns", () => {
    const persisted = (
      id: string,
      role: HarnessMessage["role"],
      position: number,
    ): HarnessMessage => ({
      id,
      session_id: "s1",
      role,
      content: id,
      parts: [],
      position,
      completed_at: "2026-10-27T10:00:00Z",
    });
    const live: HarnessMessage = {
      id: "live",
      session_id: "s1",
      role: "assistant",
      content: "",
      parts: [part("live-text", "text", "Streaming reply", 0)],
      created_at: "2026-10-27T10:05:00Z",
    };
    render(
      <ThemeProvider>
        <ChatMessages
          busy
          messages={[
            persisted("first", "user", 0),
            live,
            persisted("answer", "assistant", 1),
            persisted("follow-up", "user", 2),
          ]}
        />
      </ThemeProvider>,
    );
    const order = screen
      .getAllByTestId(/^chat-message-(first|answer|follow-up|live)$/)
      .map((row) => row.props.testID);
    expect(order).toEqual([
      "chat-message-first",
      "chat-message-answer",
      "chat-message-follow-up",
      "chat-message-live",
    ]);
  });

  it("offers scroll to latest only while the reader is away from the end", async () => {
    render(
      <ThemeProvider>
        <ChatMessages messages={[message([part("t", "text", "Answer", 0)])]} />
      </ThemeProvider>,
    );
    expect(
      screen.queryByRole("button", { name: "Scroll to latest message" }),
    ).toBeNull();

    act(() => mockNearEndListeners.forEach((listener) => listener(false)));
    fireEvent.press(
      screen.getByRole("button", { name: "Scroll to latest message" }),
    );

    expect(mockScrollToEnd).toHaveBeenCalledWith({ animated: true });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Scroll to latest message" }),
      ).toBeNull(),
    );
  });

  it("opens user actions on long press and keeps edit-and-run available", async () => {
    const onEdit = jest.fn().mockResolvedValue(undefined);
    const prompt: HarnessMessage = {
      id: "prompt",
      session_id: "s1",
      role: "user",
      content: "Original prompt",
      parts: [],
    };
    render(
      <ThemeProvider>
        <ChatMessages messages={[prompt]} onEdit={onEdit} />
      </ThemeProvider>,
    );
    expect(screen.queryByTestId("chat-edit-action-prompt")).toBeNull();
    fireEvent(screen.getByTestId("chat-message-menu-prompt"), "longPress");
    fireEvent.press(screen.getByRole("button", { name: "Edit and run again" }));
    fireEvent.changeText(
      screen.getByTestId("chat-edit-prompt"),
      "Updated prompt",
    );
    fireEvent.press(screen.getByRole("button", { name: "Save & run" }));
    await waitFor(() =>
      expect(onEdit).toHaveBeenCalledWith("prompt", "Updated prompt"),
    );
  });

  it("allows inspecting a user message while editing is unavailable", () => {
    const onEdit = jest.fn();
    render(
      <ThemeProvider>
        <ChatMessages
          disabled
          onEdit={onEdit}
          messages={[
            {
              id: "prompt",
              session_id: "s1",
              role: "user",
              content: "Saved prompt",
              parts: [],
            },
          ]}
        />
      </ThemeProvider>,
    );
    fireEvent(screen.getByTestId("chat-message-menu-prompt"), "longPress");
    expect(screen.getByRole("button", { name: "Copy message" })).toBeEnabled();
    expect(screen.queryByTestId("chat-edit-action-prompt")).toBeNull();
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("renders ordered work details and fetches omitted heavy detail only when expanded", async () => {
    const onRequestPartDetail = jest.fn().mockResolvedValue({
      ...part("tool-1", "tool", "Full output", 0),
      input: { tool: "read" },
      detail_loaded: true,
    });
    const transcript = message([
      part("text-1", "text", "Before", 0),
      {
        ...part("tool-1", "tool", "", 1),
        detail_loaded: false,
        display: { tool: "read", summary: "README.md" },
      },
      part("text-2", "text", "After", 2),
    ]);
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[transcript]}
          onRequestPartDetail={onRequestPartDetail}
        />
      </ThemeProvider>,
    );

    // Finished turns stay flat: text around the work remains visible.
    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText("After")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Explored 1 file" }));
    expect(onRequestPartDetail).not.toHaveBeenCalled();
    fireEvent.press(screen.getByRole("button", { name: "read" }));

    await waitFor(() =>
      expect(onRequestPartDetail).toHaveBeenCalledWith("s1", "tool-1"),
    );
    expect(screen.getByText("Full output")).toBeTruthy();
    expect(onRequestPartDetail).toHaveBeenCalledTimes(1);
  });

  it("groups all contiguous work, including edits and failures, between text", () => {
    const transcript = message([
      part("think", "reasoning", "Plan", 0),
      { ...part("read", "tool", "", 1), tool: "read", title: "Read a.ts" },
      { ...part("edit", "tool", "", 2), tool: "edit", call_id: "c1" },
      {
        ...part("patch", "patch", "", 3),
        call_id: "c1",
        display: { path: "/workspace/a.ts", additions: 2, deletions: 1 },
      },
      { ...part("bash", "tool", "", 4), tool: "bash", state: "error" },
      part("text", "text", "Done", 5),
      { ...part("grep", "tool", "", 6), tool: "grep" },
    ]);
    transcript.completed_at = "2026-10-29T12:00:00Z";
    render(
      <ThemeProvider>
        <ChatMessages messages={[transcript]} />
      </ThemeProvider>,
    );

    const labels = screen
      .getAllByTestId(/^chat-(activity|part)-/)
      .map((row) => row.props.testID);
    expect(labels).toEqual([
      "chat-activity-think",
      "chat-part-text",
      "chat-activity-grep",
    ]);
    fireEvent.press(
      screen.getByRole("button", {
        name: "Explored 1 file, edited 1 file, ran 1 command, 1 failed",
      }),
    );
    expect(
      screen.getByRole("button", { name: "Edited a.ts, +2 −1" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "bash, failed" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Explored 1 search" }),
    ).toBeTruthy();
  });

  it("mounts large expanded groups one page at a time", () => {
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[
            message(
              Array.from({ length: 85 }, (_, index) => ({
                ...part(`read-${index}`, "tool", "", index),
                tool: "read",
              })),
            ),
          ]}
        />
      </ThemeProvider>,
    );
    fireEvent.press(screen.getByRole("button", { name: "Explored 85 files" }));
    expect(screen.getAllByTestId(/^chat-work-row-/)).toHaveLength(80);
    fireEvent.press(screen.getByRole("button", { name: "Show 5 more" }));
    expect(screen.getAllByTestId(/^chat-work-row-/)).toHaveLength(85);
    expect(
      screen.queryByRole("button", { name: /^Show \d+ more$/ }),
    ).toBeNull();
  });

  it("expands a lone thought straight to its text", () => {
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[
            message([
              part("think", "reasoning", "Check the renderer first", 0),
              part("text", "text", "Answer", 1),
            ]),
          ]}
        />
      </ThemeProvider>,
    );
    expect(screen.queryByText("Check the renderer first")).toBeNull();
    fireEvent.press(screen.getByRole("button", { name: "Thought" }));
    expect(screen.getByText("Check the renderer first")).toBeTruthy();
  });

  it.each(["child", undefined])(
    "uses only the explicit subtask chat link: %s",
    (childId) => {
      const onOpenSubtask = jest.fn();
      render(
        <ThemeProvider>
          <ChatMessages
            messages={[
              message([
                {
                  ...part("subtask", "subtask", "", 0),
                  title: "Find renderer",
                  display: { child_session_id: childId },
                },
              ]),
            ]}
            sessions={[
              {
                id: "child",
                workspace_id: "workspace-1",
                parent_id: "s1",
                title: "Find renderer",
                mode: "build",
                agent_name: "explore",
                model: "m",
                status: "idle",
                cost: 0,
                tokens: {},
              },
            ]}
            onOpenSubtask={onOpenSubtask}
          />
        </ThemeProvider>,
      );
      fireEvent.press(screen.getByRole("button", { name: "Delegated 1 task" }));
      const row = screen.getByTestId("chat-subtask-subtask");
      if (childId) {
        fireEvent.press(row);
        expect(onOpenSubtask).toHaveBeenCalledWith(childId);
      } else {
        expect(row).toBeDisabled();
        fireEvent.press(row);
        expect(onOpenSubtask).not.toHaveBeenCalled();
      }
    },
  );

  it("renders delivered steer blocks in the native user bubble pattern", () => {
    const transcript = message([
      part("text-before", "text", "Working on it", 0),
      {
        ...part("steer-1", "steer", "Use pnpm instead", 1),
        meta: { steer_id: "queued-1" },
      },
    ]);
    transcript.completed_at = "2026-10-18T12:00:00Z";

    render(
      <ThemeProvider>
        <ChatMessages messages={[transcript]} />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("chat-steer-steer-1")).toBeTruthy();
    expect(screen.getByTestId("chat-steer-content-steer-1")).toBeTruthy();
    expect(screen.getByText("Use pnpm instead")).toBeTruthy();
    expect(screen.getByText("Sent while working")).toBeTruthy();
  });

  const todo = (
    id: string,
    status: HarnessTodo["status"],
    order: number,
  ): HarnessTodo => ({
    id,
    content: `${id} task`,
    status,
    priority: "medium",
    order,
  });

  it("shows the to-do list in place of the latest to-do call", () => {
    const todoCall = (id: string, position: number): HarnessPart => ({
      ...part(id, "tool", "", position),
      tool: "todowrite",
      title: "Update todos (2)",
    });
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[
            message([
              todoCall("todo-1", 0),
              { ...part("read", "tool", "", 1), tool: "read" },
              todoCall("todo-2", 2),
              { ...part("grep", "tool", "", 3), tool: "grep" },
              part("text", "text", "Answer", 4),
            ]),
          ]}
          todos={[
            todo("second", "pending", 1),
            todo("first", "in_progress", 0),
          ]}
        />
      </ThemeProvider>,
    );

    const order = screen
      .getAllByTestId(/^chat-(activity|part)-|^chat-todos$/)
      .map((row) => row.props.testID);
    expect(order).toEqual([
      "chat-activity-todo-1",
      "chat-todos",
      "chat-activity-grep",
      "chat-part-text",
    ]);
    expect(
      screen.getByRole("button", { name: "Explored 1 file, updated to-dos" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "To-dos, 0 of 2 done" }),
    ).toBeTruthy();
    expect(
      screen
        .getAllByTestId(/^chat-todo-/)
        .map((row) => row.props.accessibilityLabel),
    ).toEqual(["first task, in progress", "second task, pending"]);
  });

  it("appends plan updates without a to-do call and folds finished lists", () => {
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[message([part("text", "text", "Answer", 0)])]}
          todos={[
            todo("first", "completed", 0),
            todo("second", "cancelled", 1),
          ]}
        />
      </ThemeProvider>,
    );

    const header = screen.getByRole("button", { name: "To-dos, 1 of 2 done" });
    expect(screen.queryByText("first task")).toBeNull();
    fireEvent.press(header);
    expect(screen.getByText("first task")).toBeTruthy();
    expect(screen.getByText("second task")).toBeTruthy();
  });
});

describe("user message context", () => {
  it("turns workspace file mentions into ordered chips instead of raw tokens", () => {
    render(
      <ThemeProvider>
        <UserMessageContent
          markdown={"Open @file:/workspace/src/main.ts after this."}
        />
      </ThemeProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Open file /workspace/src/main.ts" }),
    ).toBeTruthy();
    expect(screen.queryByText(/@file:/)).toBeNull();
  });
});
