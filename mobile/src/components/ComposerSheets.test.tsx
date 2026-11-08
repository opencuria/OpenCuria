import React, { useState } from "react";
import { Text } from "react-native";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type {
  HarnessPermissionRequest,
  HarnessQuestionRequest,
} from "@opencuria/chat-core/harness";
import type { MentionCandidate } from "@opencuria/chat-core/harnessMentions";
import { QUESTION_MULTI_SELECT_SEPARATOR as NUL } from "@opencuria/chat-core/harnessQuestion";
import { ChatComposer } from "./ChatComposer";
import { ChatLayout, useChatLayout } from "./ChatLayout";
import type { ComposerGates } from "./ComposerSheets";
import { ThemeProvider } from "../ui/theme";

jest.mock("lucide-react-native", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const icon = (props: Record<string, unknown>) =>
    React.createElement(ReactNative.View, props);
  return new Proxy({}, { get: () => icon });
});
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

const permission: HarnessPermissionRequest = {
  request_id: "permission-1",
  session_id: "session-1",
  workspace_id: "workspace-1",
  tool: "bash",
  pattern: "pwd",
  title: "Run command",
};
const question: HarnessQuestionRequest = {
  request_id: "question-1",
  session_id: "session-1",
  workspace_id: "workspace-1",
  questions: [
    {
      question: "Choose outputs",
      multiple: true,
      options: [{ label: "Text" }, { label: "Images" }],
    },
  ],
};
const file: MentionCandidate = {
  kind: "file",
  label: "a.ts",
  insert: "file:a.ts",
};

function gates(overrides: Partial<ComposerGates> = {}): ComposerGates {
  return {
    questions: [],
    permissions: [],
    onSubmitQuestion: jest.fn(),
    onSkipQuestion: jest.fn(),
    onResolvePermission: jest.fn(),
    ...overrides,
  };
}

function Composer({
  gates: pending,
  proposals,
}: {
  gates: ComposerGates;
  proposals?: MentionCandidate[];
}): React.JSX.Element {
  const [draft, setDraft] = useState("");
  return (
    <ChatComposer
      draft={draft}
      onDraftChange={(value) => setDraft(value)}
      onSend={jest.fn()}
      proposals={proposals}
      gates={pending}
    />
  );
}

function renderComposer(
  pending: ComposerGates,
  proposals?: MentionCandidate[],
) {
  const view = render(
    <ThemeProvider>
      <Composer gates={pending} proposals={proposals} />
    </ThemeProvider>,
  );
  return {
    ...view,
    rerenderGates: (next: ComposerGates) =>
      view.rerender(
        <ThemeProvider>
          <Composer gates={next} proposals={proposals} />
        </ThemeProvider>,
      ),
  };
}

describe("ComposerSheetStack", () => {
  it("puts the question on top and lets the permission peek beneath it", () => {
    renderComposer(gates({ questions: [question], permissions: [permission] }));

    expect(screen.getByTestId("composer-question-sheet")).toBeTruthy();
    expect(screen.queryByTestId("composer-permission-sheet")).toBeNull();
    expect(screen.getAllByTestId("composer-sheet-peek")).toHaveLength(1);
  });

  it("submits multi-select choices and an own answer NUL-joined", async () => {
    const onSubmitQuestion = jest.fn().mockResolvedValue(undefined);
    renderComposer(gates({ questions: [question], onSubmitQuestion }));

    fireEvent.press(screen.getByRole("checkbox", { name: "Text" }));
    fireEvent.press(screen.getByRole("checkbox", { name: "Images" }));
    fireEvent.changeText(
      screen.getByLabelText("Your answer for Choose outputs"),
      "Audio",
    );
    fireEvent.press(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() =>
      expect(onSubmitQuestion).toHaveBeenCalledWith("question-1", [
        `Text${NUL}Images${NUL}Audio`,
      ]),
    );
  });

  it("keeps answers while mention suggestions cover the question", () => {
    renderComposer(gates({ questions: [question] }), [file]);

    fireEvent.press(screen.getByRole("checkbox", { name: "Text" }));
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "see @");

    expect(screen.getByTestId("composer-mention-sheet")).toBeTruthy();
    expect(screen.queryByTestId("composer-question-sheet")).toBeNull();
    expect(screen.getAllByTestId("composer-sheet-peek")).toHaveLength(1);

    fireEvent.press(screen.getByLabelText("Add a.ts"));

    expect(screen.queryByTestId("composer-mention-sheet")).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: "Text" }).props.accessibilityState,
    ).toMatchObject({ checked: true });
  });

  it("folds into its header and unfolds for the next request", () => {
    const onSkipQuestion = jest.fn();
    const { rerenderGates } = renderComposer(
      gates({ questions: [question], onSkipQuestion }),
    );

    fireEvent.press(screen.getByRole("button", { name: "Hide questions" }));
    expect(screen.getByText("Questions")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();

    rerenderGates(
      gates({
        questions: [{ ...question, request_id: "question-2" }],
        onSkipQuestion,
      }),
    );
    fireEvent.press(screen.getByRole("button", { name: "Skip" }));
    expect(onSkipQuestion).toHaveBeenCalledWith("question-2");
  });

  it("resolves permissions and stays open after a failed decision", async () => {
    const onResolvePermission = jest
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(undefined);
    renderComposer(gates({ permissions: [permission], onResolvePermission }));

    expect(screen.getByText("pwd")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Approve once" }));
    await waitFor(() =>
      expect(onResolvePermission).toHaveBeenCalledWith("permission-1", "once"),
    );

    fireEvent.press(screen.getByRole("button", { name: "Always allow" }));
    await waitFor(() =>
      expect(onResolvePermission).toHaveBeenLastCalledWith(
        "permission-1",
        "always",
      ),
    );
  });
});

function DockOffset(): React.JSX.Element {
  return <Text testID="dock-offset">{useChatLayout().dockOffset}</Text>;
}

describe("ComposerSheetStack in the chat layout", () => {
  function renderLayout(pending: ComposerGates) {
    const tree = (next: ComposerGates) => (
      <ThemeProvider>
        <ChatLayout composer={<Composer gates={next} />}>
          <DockOffset />
        </ChatLayout>
      </ThemeProvider>
    );
    const view = render(tree(pending));
    return {
      rerenderGates: (next: ComposerGates) => view.rerender(tree(next)),
    };
  }

  it("keeps the transcript clear of the sheets until they resolve", () => {
    const { rerenderGates } = renderLayout(gates({ questions: [question] }));
    // Estimated composer (104) and resting inset (12).
    expect(screen.getByTestId("dock-offset")).toHaveTextContent("116");

    fireEvent(screen.getByTestId("composer-sheet-stack"), "layout", {
      nativeEvent: { layout: { x: 0, y: 0, width: 360, height: 224 } },
    });
    // The composer overlaps the sheet's lower 24 points.
    expect(screen.getByTestId("dock-offset")).toHaveTextContent("316");

    rerenderGates(gates());
    expect(screen.getByTestId("dock-offset")).toHaveTextContent("116");
  });
});
