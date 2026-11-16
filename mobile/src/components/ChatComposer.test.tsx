import React from "react";
import {
  act,
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
  it("keeps the same editor and layout through loading, unavailable and running states", () => {
    const { rerenderComposer } = renderComposer({ contextKey: "new" });
    const input = screen.getByTestId("chat-composer-input");
    const inputStyle = input.props.style;

    rerenderComposer({ disabled: true, contextKey: "loading" });
    expect(screen.getByTestId("chat-composer-input")).toBe(input);
    expect(input.props.style).toEqual(inputStyle);
    expect(screen.queryByText(/not available right now/i)).toBeNull();

    rerenderComposer({ busy: true, contextKey: "existing-chat" });
    expect(screen.getByTestId("chat-composer-input")).toBe(input);
    expect(input.props.style).toEqual(inputStyle);
    expect(screen.getByTestId("chat-composer-stop")).toBeTruthy();

    rerenderComposer({ contextKey: "existing-chat" });
    expect(screen.getByTestId("chat-composer-input")).toBe(input);
    expect(input.props.style).toEqual(inputStyle);
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

  it("names a new chat's workspace above the input and changes it from a menu", async () => {
    const onSelect = jest.fn();
    let finishSend: () => void = () => undefined;
    const workspace = {
      name: "Study-Guru",
      actions: [
        { id: "a", title: "Study-Guru", selected: true },
        { id: "b", title: "Docs", subtitle: "Stopped", selected: false },
      ],
      onSelect,
    };
    const { rerenderComposer } = renderComposer({
      workspace,
      onSend: () => new Promise<void>((resolve) => (finishSend = resolve)),
    });
    const picker = screen.getByRole("button", {
      name: "Workspace: Study-Guru",
    });
    expect(picker).toHaveTextContent("Study-Guru");
    fireEvent.press(picker);
    fireEvent.press(screen.getByRole("button", { name: "Docs" }));
    expect(onSelect).toHaveBeenCalledWith("b");

    // A prompt on its way cannot move to another workspace.
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "ship it");
    fireEvent.press(screen.getByTestId("chat-composer-send"));
    fireEvent.press(picker);
    expect(screen.getByRole("button", { name: "Docs" })).toBeDisabled();
    await act(async () => finishSend());

    rerenderComposer({});
    expect(screen.queryByTestId("chat-composer-workspace")).toBeNull();
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

  it("keeps writing possible but sending and attaching closed when it cannot send", () => {
    const { onSend } = renderComposer({ canSend: false });
    const input = screen.getByTestId("chat-composer-input");
    expect(input.props.editable).toBe(true);
    fireEvent.changeText(input, "Draft for later");
    expect(screen.getByTestId("chat-composer-send")).toBeDisabled();
    fireEvent.press(screen.getByTestId("chat-composer-send"));
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

  it("replaces the editor when a sent draft is cleared, so a grown composer shrinks", async () => {
    renderComposer({ onSend: jest.fn().mockResolvedValue(undefined) });
    const input = screen.getByTestId("chat-composer-input");
    fireEvent.changeText(input, "line one\nline two\nline three\nline four");
    expect(screen.getByTestId("chat-composer-input")).toBe(input);

    fireEvent.press(screen.getByTestId("chat-composer-send"));

    const cleared = screen.getByTestId("chat-composer-input");
    expect(cleared).not.toBe(input);
    expect(cleared.props.value).toBe("");
    await waitFor(() =>
      expect(screen.getByTestId("chat-composer-send")).toBeTruthy(),
    );
    expect(screen.getByTestId("chat-composer-input")).toBe(cleared);
  });

  it("keeps the focused editor while typing and inserting a candidate", async () => {
    const candidate = {
      kind: "agent" as const,
      label: "explore",
      insert: "agent:explore",
    };
    renderComposer({ proposals: [candidate] });
    const input = screen.getByTestId("chat-composer-input");

    fireEvent.changeText(input, "first\nsecond @");
    fireEvent.press(await screen.findByRole("button", { name: "Add explore" }));

    expect(screen.getByTestId("chat-composer-input")).toBe(input);
    expect(input.props.value).toBe("first\nsecond @agent:explore ");
  });

  it("replaces the editor when the host resets a long draft", () => {
    let reset: () => void = () => undefined;
    function Host(): React.JSX.Element {
      const [draft, setDraft] = React.useState("");
      reset = () => setDraft("");
      return (
        <ThemeProvider>
          <ChatComposer
            draft={draft}
            onDraftChange={setDraft}
            onSend={jest.fn()}
          />
        </ThemeProvider>
      );
    }
    render(<Host />);
    const input = screen.getByTestId("chat-composer-input");
    fireEvent.changeText(input, "a long draft\nacross\nseveral lines");

    act(() => reset());

    const cleared = screen.getByTestId("chat-composer-input");
    expect(cleared).not.toBe(input);
    expect(cleared.props.value).toBe("");
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

describe("ChatComposer steering and send later", () => {
  /** A local date a few days ahead, inside the one-year scheduling window. */
  const soon = new Date();
  soon.setDate(soon.getDate() + 3);
  soon.setHours(9, 0, 0, 0);
  const soonDate = [
    soon.getFullYear(),
    String(soon.getMonth() + 1).padStart(2, "0"),
    String(soon.getDate()).padStart(2, "0"),
  ].join("-");
  const scheduled = {
    id: "later-1",
    session_id: "session-1",
    content: "Check the deploy",
    send_at: soon.toISOString(),
    status: "pending" as const,
    created_at: new Date().toISOString(),
  };

  function queue(
    overrides: Partial<
      NonNullable<React.ComponentProps<typeof ChatComposer>["queue"]>
    > = {},
  ) {
    return {
      steers: [],
      scheduled: [],
      onWithdrawSteer: jest.fn(),
      onSendNow: jest.fn(),
      onReschedule: jest.fn(),
      onEdit: jest.fn(),
      onDelete: jest.fn(),
      ...overrides,
    };
  }

  it("steers with a draft while the agent works and keeps Stop at hand", async () => {
    const onStop = jest.fn();
    const { onSend, rerenderComposer } = renderComposer({
      busy: true,
      steering: true,
      onStop,
    });
    expect(screen.getByTestId("chat-composer-stop")).toBeTruthy();
    expect(screen.queryByTestId("chat-composer-send")).toBeNull();

    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Use pnpm");
    expect(screen.getByTestId("chat-composer-send")).toBeEnabled();
    fireEvent.press(screen.getByTestId("chat-composer-stop"));
    expect(onStop).toHaveBeenCalledTimes(1);
    fireEvent.press(screen.getByTestId("chat-composer-send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        expect.objectContaining({ text: "Use pnpm" }),
      ),
    );

    // A send that is still on its way cannot be followed up yet.
    rerenderComposer({ busy: true, steering: false, onStop });
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Again");
    expect(screen.queryByTestId("chat-composer-send")).toBeNull();
    expect(screen.getByTestId("chat-composer-stop")).toBeTruthy();
  });

  it("schedules the draft from the send button's long-press menu", async () => {
    const onSchedule = jest.fn(async () => undefined);
    const { onDraftChange, onSend } = renderComposer({ onSchedule });
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Check CI");

    fireEvent(screen.getByTestId("chat-composer-send"), "longPress");
    fireEvent.press(screen.getByRole("button", { name: /^Tomorrow, / }));

    await waitFor(() =>
      expect(onSchedule).toHaveBeenCalledWith(
        expect.objectContaining({ text: "Check CI" }),
        expect.any(Date),
      ),
    );
    const sendAt = (onSchedule.mock.calls[0] as unknown[])[1] as Date;
    expect([sendAt.getHours(), sendAt.getMinutes()]).toEqual([9, 0]);
    expect(onSend).not.toHaveBeenCalled();
    expect(onDraftChange).toHaveBeenLastCalledWith("", { start: 0, end: 0 });
  });

  it("gives the draft back when scheduling fails", async () => {
    const onSchedule = jest.fn(async () => {
      throw new Error("offline");
    });
    renderComposer({ onSchedule });
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Ship it");

    fireEvent(screen.getByTestId("chat-composer-send"), "longPress");
    fireEvent.press(screen.getByRole("button", { name: "In 1 hour" }));

    expect(await screen.findByText(/could not be scheduled/i)).toBeTruthy();
    expect(screen.getByTestId("chat-composer-input").props.value).toBe(
      "Ship it",
    );
  });

  it("offers no send-later menu without a draft", () => {
    renderComposer({ onSchedule: jest.fn() });
    fireEvent(screen.getByTestId("chat-composer-send"), "longPress");
    expect(screen.queryByRole("button", { name: "In 1 hour" })).toBeNull();
  });

  it("schedules at a custom date and time from native pickers", async () => {
    const onSchedule = jest.fn(async () => undefined);
    renderComposer({ onSchedule });
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Report");

    fireEvent(screen.getByTestId("chat-composer-send"), "longPress");
    fireEvent.press(
      screen.getByRole("button", { name: "Choose date & time…" }),
    );
    fireEvent.changeText(screen.getByTestId("send-later-date"), soonDate);
    fireEvent.changeText(screen.getByTestId("send-later-time"), "07:45");
    expect(screen.getByText(/^Sends /)).toBeTruthy();
    fireEvent.press(screen.getByTestId("send-later-confirm"));

    await waitFor(() =>
      expect(onSchedule).toHaveBeenCalledWith(
        expect.objectContaining({ text: "Report" }),
        new Date(soon.getFullYear(), soon.getMonth(), soon.getDate(), 7, 45),
      ),
    );
  });

  it("refuses a past custom time", () => {
    renderComposer({ onSchedule: jest.fn() });
    fireEvent.changeText(screen.getByTestId("chat-composer-input"), "Report");
    fireEvent(screen.getByTestId("chat-composer-send"), "longPress");
    fireEvent.press(
      screen.getByRole("button", { name: "Choose date & time…" }),
    );
    fireEvent.changeText(screen.getByTestId("send-later-date"), "2001-03-04");
    expect(screen.getByText("Choose a time in the future.")).toBeTruthy();
    expect(screen.getByTestId("send-later-confirm")).toBeDisabled();
  });

  it("reschedules a scheduled message from its menu", async () => {
    const outgoing = queue({ scheduled: [scheduled] });
    renderComposer({ queue: outgoing });

    fireEvent.press(screen.getByTestId("composer-scheduled-menu-later-1"));
    fireEvent.press(screen.getByRole("button", { name: "Change time" }));
    fireEvent.changeText(screen.getByTestId("send-later-time"), "10:30");
    fireEvent.press(screen.getByTestId("send-later-confirm"));

    await waitFor(() =>
      expect(outgoing.onReschedule).toHaveBeenCalledWith(
        "later-1",
        expect.any(Date),
      ),
    );
    const sendAt = (
      outgoing.onReschedule.mock.calls[0] as unknown[]
    )[1] as Date;
    expect([sendAt.getHours(), sendAt.getMinutes()]).toEqual([10, 30]);
  });
});
