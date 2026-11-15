import React from "react";
import { Modal } from "react-native";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { ThemeProvider } from "./theme";
import { ToastHost } from "./ToastHost";
import { currentToast, dismissToast, showToast } from "./toast";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 20, right: 0, bottom: 0, left: 0 }),
}));

beforeEach(() => jest.useFakeTimers());
afterEach(() => {
  act(() => dismissToast());
  jest.useRealTimers();
});

function App({ sheet }: { sheet: boolean }) {
  return (
    <ThemeProvider>
      <ToastHost />
      {sheet ? (
        <Modal visible testID="sheet">
          <ToastHost />
        </Modal>
      ) : null}
    </ThemeProvider>
  );
}

it("shows a toast once, in the screen or sheet in front, and hides it on its own", () => {
  const { rerender } = render(<App sheet />);
  act(() => {
    showToast({ tone: "success", title: "Pushed to origin/main" });
  });
  expect(screen.getAllByTestId("toast")).toHaveLength(1);
  expect(
    screen.getByTestId("sheet").findByProps({ testID: "toast" }),
  ).toBeTruthy();

  // Closing the sheet hands the toast to the screen beneath.
  rerender(<App sheet={false} />);
  expect(screen.getAllByTestId("toast")).toHaveLength(1);

  act(() => jest.advanceTimersByTime(3000));
  expect(currentToast()).toBeNull();
});

it("replaces the visible toast and dismisses on tap", () => {
  render(<App sheet={false} />);
  act(() => {
    showToast({ tone: "error", title: "Push failed", message: "rejected" });
    showToast({ tone: "info", title: "Everything up to date" });
  });
  expect(screen.getByLabelText("Everything up to date")).toBeTruthy();
  expect(screen.queryByLabelText("Push failed, rejected")).toBeNull();
  fireEvent(screen.getByTestId("toast"), "accessibilityAction", {
    nativeEvent: { actionName: "activate" },
  });
  expect(currentToast()).toBeNull();
});
