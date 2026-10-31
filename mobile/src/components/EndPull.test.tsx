import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { ChatMessages } from "./ChatMessages";
import { ThemeProvider } from "../ui/theme";

const mockImpact = jest.fn().mockResolvedValue(undefined);
jest.mock("expo-haptics", () => ({
  impactAsync: (...args: unknown[]) => mockImpact(...args),
  performAndroidHapticsAsync: jest.fn().mockResolvedValue(undefined),
  ImpactFeedbackStyle: { Medium: "medium" },
  AndroidHaptics: { Confirm: "confirm" },
}));
jest.mock("lucide-react-native", () => {
  const React = require("react");
  const ReactNative = require("react-native");
  const icon = (props: Record<string, unknown>) =>
    React.createElement(ReactNative.View, props);
  return new Proxy({}, { get: () => icon });
});

jest.mock(
  "remend",
  () => ({ __esModule: true, default: (markdown: string) => markdown }),
  { virtual: true },
);
jest.mock("react-native-enriched-markdown", () => ({
  EnrichedMarkdownText: () => null,
}));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

/** iOS scroll metrics of a short transcript: its end is offset 0. */
function at(offset: number, velocity = 0) {
  return {
    nativeEvent: {
      contentOffset: { x: 0, y: offset },
      contentSize: { width: 390, height: 500 },
      layoutMeasurement: { width: 390, height: 800 },
      contentInset: { top: 0, left: 0, right: 0, bottom: 0 },
      velocity: { x: 0, y: velocity },
    },
  };
}

function renderTranscript(onPullUpAtEnd = jest.fn()) {
  render(
    <ThemeProvider>
      <ChatMessages
        messages={[]}
        onPullUpAtEnd={onPullUpAtEnd}
        pullUpLabel="Git, Terminal & Files"
        testID="transcript"
      />
    </ThemeProvider>,
  );
  return { onPullUpAtEnd, list: screen.getByTestId("transcript") };
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.setSystemTime(new Date("2026-10-31T12:00:00Z"));
  mockImpact.mockClear();
});
afterEach(() => jest.useRealTimers());

it("opens when the rubber band past the end is released, with one haptic tick", () => {
  const { onPullUpAtEnd, list } = renderTranscript();
  fireEvent(list, "scrollBeginDrag", at(4));
  fireEvent.scroll(list, at(30));
  expect(screen.queryByText("Release to open", { includeHiddenElements: true })).toBeNull();
  fireEvent.scroll(list, at(52));
  expect(screen.getByText("Release to open", { includeHiddenElements: true })).toBeTruthy();
  fireEvent(list, "scrollEndDrag", at(52));
  expect(onPullUpAtEnd).toHaveBeenCalledTimes(1);
  expect(mockImpact).toHaveBeenCalledTimes(1);
  expect(mockImpact).toHaveBeenCalledWith("medium");
});

it("opens on a quick release with less travel", () => {
  const { onPullUpAtEnd, list } = renderTranscript();
  fireEvent(list, "scrollBeginDrag", at(2));
  fireEvent.scroll(list, at(20));
  fireEvent(list, "scrollEndDrag", at(20, 1.2));
  expect(onPullUpAtEnd).toHaveBeenCalledTimes(1);
});

it("ignores a short pull and a drag that started while momentum still moved the list", () => {
  const { onPullUpAtEnd, list } = renderTranscript();
  fireEvent(list, "scrollBeginDrag", at(0));
  fireEvent.scroll(list, at(20));
  fireEvent(list, "scrollEndDrag", at(20));
  expect(onPullUpAtEnd).not.toHaveBeenCalled();

  act(() => jest.advanceTimersByTime(1000));
  fireEvent(list, "momentumScrollBegin", at(0));
  fireEvent.scroll(list, at(0));
  fireEvent(list, "momentumScrollEnd", at(0));
  fireEvent(list, "scrollBeginDrag", at(0));
  fireEvent.scroll(list, at(80));
  fireEvent(list, "scrollEndDrag", at(80));
  expect(onPullUpAtEnd).not.toHaveBeenCalled();
});

it("does not open when the drag first scrolls back into the transcript", () => {
  const { onPullUpAtEnd, list } = renderTranscript();
  fireEvent(list, "scrollBeginDrag", at(0));
  fireEvent.scroll(list, at(-60));
  fireEvent.scroll(list, at(70));
  fireEvent(list, "scrollEndDrag", at(70));
  expect(onPullUpAtEnd).not.toHaveBeenCalled();
});

it("needs the transcript to rest a moment, so quick repeated flicks never open", () => {
  const { onPullUpAtEnd, list } = renderTranscript();
  fireEvent(list, "scrollBeginDrag", at(0));
  fireEvent.scroll(list, at(10));
  fireEvent(list, "scrollEndDrag", at(10));
  act(() => jest.advanceTimersByTime(300));
  fireEvent(list, "scrollBeginDrag", at(0));
  fireEvent.scroll(list, at(60));
  fireEvent(list, "scrollEndDrag", at(60, 1.5));
  expect(onPullUpAtEnd).not.toHaveBeenCalled();

  act(() => jest.advanceTimersByTime(600));
  fireEvent(list, "scrollBeginDrag", at(0));
  fireEvent.scroll(list, at(60));
  fireEvent(list, "scrollEndDrag", at(60));
  expect(onPullUpAtEnd).toHaveBeenCalledTimes(1);
});
