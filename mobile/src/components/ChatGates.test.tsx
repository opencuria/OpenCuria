import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { ChatGates } from "./ChatGates";
import { ThemeProvider } from "../ui/theme";
import type {
  HarnessPermissionRequest,
  HarnessQuestionRequest,
} from "@opencuria/chat-core/harness";

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

describe("ChatGates", () => {
  it("mounts one native gate when permission and question requests coexist", () => {
    render(
      <ThemeProvider>
        <ChatGates
          permissions={[permission]}
          questions={[question]}
          onResolvePermission={jest.fn()}
          onSubmitQuestion={jest.fn()}
          onSkipQuestion={jest.fn()}
        />
      </ThemeProvider>,
    );
    expect(screen.getByTestId("chat-gates-permission")).toBeTruthy();
    expect(screen.queryByTestId("chat-gates-question")).toBeNull();
  });

  it("submits multi-select choices using the NUL protocol separator", async () => {
    const onSubmitQuestion = jest.fn().mockResolvedValue(undefined);
    render(
      <ThemeProvider>
        <ChatGates
          permissions={[]}
          questions={[question]}
          onResolvePermission={jest.fn()}
          onSubmitQuestion={onSubmitQuestion}
          onSkipQuestion={jest.fn()}
        />
      </ThemeProvider>,
    );

    fireEvent.press(screen.getByRole("checkbox", { name: "Text" }));
    fireEvent.press(screen.getByRole("checkbox", { name: "Images" }));
    fireEvent.press(screen.getByRole("button", { name: "Submit answers" }));

    await waitFor(() =>
      expect(onSubmitQuestion).toHaveBeenCalledWith("question-1", [
        "Text\0Images",
      ]),
    );
  });
});
