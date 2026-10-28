import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { ChatComposer } from "./ChatComposer";
import { ThemeProvider } from "../ui/theme";

jest.mock("lucide-react-native", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const icon = (props: Record<string, unknown>) =>
    React.createElement(ReactNative.View, props);
  return new Proxy({}, { get: () => icon });
});
jest.mock("expo-image", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  return {
    Image: (props: Record<string, unknown>) =>
      React.createElement(ReactNative.Image, props),
  };
});
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

function renderComposer(
  props: Partial<React.ComponentProps<typeof ChatComposer>> = {},
) {
  const {
    draft: initialDraft = "",
    onDraftChange: _providedChange,
    onSend: providedSend,
    ...composerProps
  } = props;
  const onDraftChange = jest.fn();
  const onSend = providedSend ?? jest.fn();
  function ControlledComposer({
    overrides,
  }: {
    overrides: Partial<React.ComponentProps<typeof ChatComposer>>;
  }): React.JSX.Element {
    const { useState } = require("react");
    const [draft, setDraft] = useState(initialDraft);
    return (
      <ThemeProvider>
        <ChatComposer
          {...overrides}
          draft={draft}
          onDraftChange={(value, selection) => {
            onDraftChange(value, selection);
            setDraft(value);
          }}
          onSend={onSend}
        />
      </ThemeProvider>
    );
  }
  const view = render(<ControlledComposer overrides={composerProps} />);
  return {
    ...view,
    onDraftChange,
    onSend,
    rerenderComposer: (
      overrides: Partial<React.ComponentProps<typeof ChatComposer>>,
    ) => view.rerender(<ControlledComposer overrides={overrides} />),
  };
}

describe("ChatComposer", () => {
  it("shows the selected workspace above a new draft and updates it with the context", () => {
    const { rerenderComposer } = renderComposer({
      workspaceName: "EventAlertExpo",
      contextKey: "workspace-one:new",
    });

    expect(screen.getByLabelText("Workspace: EventAlertExpo")).toHaveTextContent(
      "EventAlertExpo",
    );
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Test");
    expect(screen.getByTestId("chat-composer-workspace")).toHaveTextContent(
      "EventAlertExpo",
    );

    rerenderComposer({
      workspaceName: "Another workspace",
      contextKey: "workspace-two:new",
    });
    expect(screen.getByTestId("chat-composer-workspace")).toHaveTextContent(
      "Another workspace",
    );
    expect(screen.queryByLabelText("Workspace: EventAlertExpo")).toBeNull();

    rerenderComposer({ contextKey: "workspace-two:existing-chat" });
    expect(screen.queryByTestId("chat-composer-workspace")).toBeNull();
  });

  it("shows the model and effort on one control below the input", () => {
    const onSettings = jest.fn();
    renderComposer({
      settings: {
        harness: "OpenCuria",
        model: "Workspace model",
        mode: "plan",
        effort: "High",
      },
      onSettings,
    });
    const pill = screen.getByRole("button", {
      name: "Chat settings: OpenCuria, plan, Workspace model, High",
    });
    expect(pill).toHaveTextContent("Plan · Workspace model High");
    fireEvent.press(pill);
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Hi");
    expect(screen.getAllByTestId("chat-composer-input")).toHaveLength(1);
    fireEvent.press(screen.getByTestId("chat-composer-settings"));
    expect(onSettings).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("chat-composer-send")).toBeEnabled();
  });

  it("asks for a model when none is selected", () => {
    renderComposer({
      settings: { model: "", mode: "build" },
      onSettings: jest.fn(),
    });
    expect(screen.getByTestId("chat-composer-settings")).toHaveTextContent(
      "Choose model",
    );
  });

  it("only enables sending a draft when the chat is available", () => {
    const { onSend, rerenderComposer } = renderComposer();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();

    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "  ");
    fireEvent.press(screen.getByTestId("chat-composer-send"));
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Test");
    expect(screen.getByTestId("chat-composer-send")).toBeEnabled();
    rerenderComposer({ disabled: true });
    fireEvent.press(screen.getByTestId("chat-composer-send"));
    expect(screen.getByTestId("chat-composer-send")).toBeDisabled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("sends an attachment without requiring a text draft", async () => {
    const attachment = {
      id: "document",
      uri: "file:///review.txt",
      name: "review.txt",
    };
    const { onSend } = renderComposer({ attachments: [attachment] });
    expect(screen.getByTestId("chat-composer-send")).toBeEnabled();
    fireEvent.press(screen.getByTestId("chat-composer-send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith({
        text: "",
        skillIds: [],
        attachments: [attachment],
      }),
    );
  });

  it("stops a busy run even when the composer is disabled and empty", async () => {
    const onStop = jest.fn().mockResolvedValue(undefined);
    renderComposer({ busy: true, disabled: true, onStop });

    fireEvent.press(screen.getByTestId("chat-composer-stop"));

    await waitFor(() => expect(onStop).toHaveBeenCalledTimes(1));
  });

  it("inserts an @ candidate without controlling native typing selection", async () => {
    const candidate = {
      kind: "file" as const,
      label: "src/main.ts",
      insert: "file:/workspace/src/main.ts",
    };
    const { onDraftChange } = renderComposer({ proposals: [candidate] });
    const input = screen.getByTestId("chat-composer-input");

    fireEvent.changeText(input, "@");
    fireEvent.press(
      await screen.findByRole("button", { name: "Add src/main.ts" }),
    );

    expect(onDraftChange).toHaveBeenLastCalledWith(
      "@file:/workspace/src/main.ts ",
      { start: 29, end: 29 },
    );
    expect(screen.getByTestId("chat-composer-input").props.value).toBe(
      "@file:/workspace/src/main.ts ",
    );
    expect(
      screen.getByTestId("chat-composer-input").props.selection,
    ).toBeUndefined();
  });

  it("uses native cursor updates for typing in the middle without pinning TextInput selection", () => {
    const { onDraftChange } = renderComposer({ draft: "ab" });
    const input = screen.getByTestId("chat-composer-input");

    fireEvent(input, "selectionChange", {
      nativeEvent: { selection: { start: 1, end: 1 } },
    });
    fireEvent.changeText(input, "aXb");

    expect(onDraftChange).toHaveBeenLastCalledWith("aXb", { start: 1, end: 1 });
    expect(screen.getByTestId("chat-composer-input").props.value).toBe("aXb");
    expect(
      screen.getByTestId("chat-composer-input").props.selection,
    ).toBeUndefined();
  });

  it("keeps the controlled draft and shows inline feedback when sending fails", async () => {
    const onSend = jest.fn().mockRejectedValue(new Error("offline"));
    const { onDraftChange } = renderComposer({ onSend });
    fireEvent.changeText(
      screen.getByTestId("chat-composer-input"),
      "keep this draft",
    );

    fireEvent.press(screen.getByTestId("chat-composer-send"));

    await waitFor(() =>
      expect(screen.getByTestId("chat-composer-error")).toBeTruthy(),
    );
    expect(screen.getByTestId("chat-composer-input").props.value).toBe(
      "keep this draft",
    );
    expect(onSend).toHaveBeenCalledWith({
      text: "keep this draft",
      skillIds: [],
      attachments: [],
    });
    expect(onDraftChange).toHaveBeenCalledWith("keep this draft", {
      start: 15,
      end: 15,
    });
  });

  it("clears the draft at once and closes the keyboard while a message is sent", async () => {
    const { KeyboardController } = require("react-native-keyboard-controller");
    (KeyboardController.dismiss as jest.Mock).mockClear();
    let finishSend: () => void = () => undefined;
    const onSend = jest.fn(
      () => new Promise<void>((resolve) => (finishSend = resolve)),
    );
    renderComposer({ onSend });
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "ship it");

    fireEvent.press(screen.getByTestId("chat-composer-send"));

    expect(screen.getByTestId("chat-composer-input").props.value).toBe("");
    expect(KeyboardController.dismiss).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith({
      text: "ship it",
      skillIds: [],
      attachments: [],
    });
    finishSend();
    await waitFor(() =>
      expect(screen.getByTestId("chat-composer-send")).toBeTruthy(),
    );
    expect(screen.queryByTestId("chat-composer-error")).toBeNull();
  });

  it("clears a confirmed draft without invalidating an in-flight native picker", async () => {
    function Host(): React.JSX.Element {
      const [draft, setDraft] = React.useState("confirmed prompt");
      return (
        <ThemeProvider>
          <ChatComposer
            draft={draft}
            onDraftChange={setDraft}
            onSend={async () => setDraft("")}
          />
        </ThemeProvider>
      );
    }
    render(<Host />);
    fireEvent.press(screen.getByTestId("chat-composer-send"));
    await waitFor(() =>
      expect(screen.getByTestId("chat-composer-input").props.value).toBe(""),
    );
  });

  it("preserves selected skill ids that are absent from the latest skill catalog", () => {
    renderComposer({ selectedSkillIds: ["removed-skill"], skills: [] });
    expect(screen.getByText("removed-skill")).toBeTruthy();
  });

  it("discards a picker result that resolves after the chat context changes", async () => {
    let resolvePicker:
      | ((
          files: { id: string; uri: string; name: string; kind: "image" }[],
        ) => void)
      | undefined;
    const onChoosePhotos = jest.fn(
      () =>
        new Promise<{ id: string; uri: string; name: string; kind: "image" }[]>(
          (resolve) => {
            resolvePicker = resolve;
          },
        ),
    );
    const onAttachmentsChange = jest.fn();
    const view = renderComposer({
      contextKey: "workspace-one",
      onChoosePhotos,
      onAttachmentsChange,
    });

    fireEvent.press(screen.getByTestId("chat-composer-attachments"));
    fireEvent.press(
      await screen.findByRole("button", { name: "Choose photos" }),
    );
    view.rerenderComposer({
      contextKey: "workspace-two",
      onChoosePhotos,
      onAttachmentsChange,
    });
    resolvePicker?.([
      { id: "late", uri: "file:///late.png", name: "late.png", kind: "image" },
    ]);

    await waitFor(() => expect(onChoosePhotos).toHaveBeenCalledTimes(1));
    expect(onAttachmentsChange).not.toHaveBeenCalled();
  });
});
