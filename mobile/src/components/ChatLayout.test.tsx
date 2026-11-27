import React from "react";
import { Text, View } from "react-native";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { KeyboardController } from "react-native-keyboard-controller";
import { makeMutable } from "react-native-reanimated";
import { ChatLayout, useChatLayout, type ChatLayoutValue } from "./ChatLayout";
import { HiddenBottomContext } from "../ui/chromeInset";
import { ThemeProvider } from "../ui/theme";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 34, left: 0 }),
}));

function Composer(): React.JSX.Element {
  const { onComposerLayout } = useChatLayout();
  return <View testID="composer-core" onLayout={onComposerLayout} />;
}

function Transcript({
  scrollToEnd,
}: {
  scrollToEnd?: () => Promise<void>;
}): React.JSX.Element {
  const { dockOffset, keyboardOffset, registerScrollToEnd, dismissKeyboard } =
    useChatLayout();
  React.useEffect(
    () => (scrollToEnd ? registerScrollToEnd(scrollToEnd) : undefined),
    [registerScrollToEnd, scrollToEnd],
  );
  return (
    <View>
      <Text testID="dock-offset">{dockOffset}</Text>
      <Text testID="keyboard-offset">{keyboardOffset}</Text>
      <Text
        testID="send"
        onPress={() => dismissKeyboard({ revealLatest: true })}
      >
        Send
      </Text>
      <Text testID="close" onPress={() => dismissKeyboard()}>
        Close
      </Text>
    </View>
  );
}

function renderLayout(scrollToEnd?: () => Promise<void>, docked = true) {
  return render(
    <ThemeProvider>
      <ChatLayout composer={docked ? <Composer /> : null}>
        <Transcript scrollToEnd={scrollToEnd} />
      </ChatLayout>
    </ThemeProvider>,
  );
}

beforeEach(() => {
  (KeyboardController.dismiss as jest.Mock).mockClear();
});

describe("ChatLayout", () => {
  it("keeps the transcript clear of the docked composer and its safe area", () => {
    renderLayout();
    expect(screen.getByTestId("dock-offset")).toHaveTextContent("138");
    fireEvent(screen.getByTestId("composer-core"), "layout", {
      nativeEvent: { layout: { x: 0, y: 0, width: 360, height: 140 } },
    });
    expect(screen.getByTestId("dock-offset")).toHaveTextContent("174");
    // The open keyboard covers the home indicator area except for a small gap.
    expect(screen.getByTestId("keyboard-offset")).toHaveTextContent("26");
  });

  it("keeps the composer on the visible edge and the transcript's end above it while a sheet rests lower", async () => {
    const hidden = makeMutable(0);
    let layout: ChatLayoutValue | undefined;
    function Probe(): null {
      layout = useChatLayout();
      return null;
    }
    render(
      <ThemeProvider>
        <HiddenBottomContext.Provider value={hidden}>
          <ChatLayout composer={<Composer />}>
            <Probe />
          </ChatLayout>
        </HiddenBottomContext.Provider>
      </ThemeProvider>,
    );
    // Estimated composer (104) and the home indicator (34).
    expect(layout!.dockHeight.value).toBe(138);
    hidden.value = 240;
    // The transcript ends above the visible edge, and the dock rises to it
    // without the layout changing.
    await waitFor(() => expect(layout!.dockHeight.value).toBe(378));
    expect(layout!.dockOffset).toBe(138);
    expect(screen.getByTestId("chat-dock")).toHaveAnimatedStyle({
      transform: [{ translateY: -240 }],
    });
  });

  it("reserves only the safe area without a composer", () => {
    renderLayout(undefined, false);
    expect(screen.getByTestId("dock-offset")).toHaveTextContent("34");
  });

  it("closes the keyboard before revealing the newest message after a send", async () => {
    const order: string[] = [];
    (KeyboardController.dismiss as jest.Mock).mockImplementationOnce(() => {
      order.push("dismiss");
      return Promise.resolve();
    });
    const scrollToEnd = jest.fn(async () => {
      order.push("scroll");
    });
    renderLayout(scrollToEnd);

    fireEvent.press(screen.getByTestId("send"));

    await waitFor(() => expect(scrollToEnd).toHaveBeenCalledTimes(1));
    expect(order).toEqual(["dismiss", "scroll"]);
  });

  it("only closes the keyboard for a plain dismissal", async () => {
    const scrollToEnd = jest.fn().mockResolvedValue(undefined);
    renderLayout(scrollToEnd);

    fireEvent.press(screen.getByTestId("close"));

    await waitFor(() => expect(KeyboardController.dismiss).toHaveBeenCalled());
    expect(scrollToEnd).not.toHaveBeenCalled();
  });
});
