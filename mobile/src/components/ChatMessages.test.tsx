import React from "react";
import {
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
jest.mock("@legendapp/list/react-native", () => {
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
      scrollToEnd: jest.fn().mockResolvedValue(undefined),
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
  return { LegendList: MockList };
});
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

describe("ChatMessages", () => {
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

    fireEvent.press(
      screen.getByRole("button", { name: /Expand work details/ }),
    );
    fireEvent.press(screen.getByRole("button", { name: /Expand read/ }));

    await waitFor(() =>
      expect(onRequestPartDetail).toHaveBeenCalledWith("s1", "tool-1"),
    );
    expect(screen.getByText("Full output")).toBeTruthy();
    expect(onRequestPartDetail).toHaveBeenCalledTimes(1);
  });

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

  it("shows all todos in an explicit task sheet", () => {
    const todos: HarnessTodo[] = [
      {
        id: "one",
        content: "First task",
        status: "in_progress",
        priority: "high",
        order: 0,
      },
      {
        id: "two",
        content: "Second task",
        status: "pending",
        priority: "normal",
        order: 1,
      },
    ];
    render(
      <ThemeProvider>
        <ChatMessages
          messages={[message([part("text-1", "text", "Answer", 0)])]}
          todos={todos}
        />
      </ThemeProvider>,
    );

    fireEvent.press(screen.getByRole("button", { name: /Show tasks/ }));

    expect(screen.getByText("First task")).toBeTruthy();
    expect(screen.getByText("Second task")).toBeTruthy();
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
