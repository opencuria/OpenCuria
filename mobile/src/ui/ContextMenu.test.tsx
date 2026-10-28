import React from "react";
import { Pressable, Text } from "react-native";
import {
  fireEvent,
  render,
  screen,
  userEvent,
} from "@testing-library/react-native";
import { NativeContextMenu } from "./ContextMenu.ios";
import { ThemeProvider } from "./theme";

jest.unmock("./ContextMenu.ios");

// Model the native hit-testing boundary that the old wrapper used: MenuView
// owns touches instead of forwarding them to a nested React Native Pressable.
jest.mock("@react-native-menu/menu", () => ({
  MenuView: ({ children }: React.PropsWithChildren) => {
    const { View } = require("react-native");
    return <View pointerEvents="box-only">{children}</View>;
  },
}));

// Keep NativeContextMenu real and mock only the SwiftUI native controls.
jest.mock("@expo/ui/swift-ui", () => {
  const ReactRuntime = require("react") as typeof React;
  const { Pressable, Text, View } = require("react-native");
  const Container = ({ children }: React.PropsWithChildren) => (
    <View>{children}</View>
  );
  function Items({ children }: React.PropsWithChildren) {
    return <View>{children}</View>;
  }
  function ContextMenu({ children }: React.PropsWithChildren) {
    const [open, setOpen] = ReactRuntime.useState(false);
    return (
      <View testID="native-context-menu" onLongPress={() => setOpen(true)}>
        {ReactRuntime.Children.toArray(children).filter(
          (child) =>
            open ||
            !ReactRuntime.isValidElement(child) ||
            child.type !== Items,
        )}
      </View>
    );
  }
  ContextMenu.Trigger = Container;
  ContextMenu.Items = Items;
  return {
    Host: Container,
    RNHostView: Container,
    Section: Container,
    ContextMenu,
    Button: ({
      label,
      onPress,
      modifiers,
    }: {
      label: string;
      onPress: () => void;
      modifiers: { disabled: boolean }[];
    }) => (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={label}
        disabled={modifiers.some((modifier) => modifier.disabled)}
        onPress={onPress}
      >
        <Text>{label}</Text>
      </Pressable>
    ),
  };
});
jest.mock("@expo/ui/swift-ui/modifiers", () => ({
  disabled: (value: boolean) => ({ disabled: value }),
}));

function setup(actionsDisabled = false) {
  const onPress = jest.fn();
  const onSelect = jest.fn();
  render(
    <ThemeProvider>
      <NativeContextMenu
        label="Test chat"
        actions={[
          { id: "rename", title: "Rename", disabled: actionsDisabled },
          { id: "read", title: "Mark as read" },
          {
            id: "delete",
            title: "Delete",
            destructive: true,
            disabled: actionsDisabled,
          },
        ]}
        onSelect={onSelect}
      >
        {() => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Open test chat"
            onPress={onPress}
            onLongPress={() => undefined}
          >
            <Text>Test chat</Text>
          </Pressable>
        )}
      </NativeContextMenu>
    </ThemeProvider>,
  );
  return { onPress, onSelect };
}

it("lets a tap reach the chat without opening its context menu", async () => {
  const { onPress, onSelect } = setup();
  await userEvent.press(screen.getByRole("button", { name: "Open test chat" }));
  expect(onPress).toHaveBeenCalledTimes(1);
  expect(onSelect).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Rename" })).toBeNull();
});

it("dispatches native long-press menu actions without opening the chat", async () => {
  const { onPress, onSelect } = setup();
  await userEvent.longPress(
    screen.getByRole("button", { name: "Open test chat" }),
  );
  expect(onPress).not.toHaveBeenCalled();
  // UIKit presents the menu; this event models that native boundary.
  fireEvent(screen.getByTestId("native-context-menu"), "longPress");
  await userEvent.press(screen.getByRole("button", { name: "Rename" }));
  expect(onSelect).toHaveBeenCalledWith("rename");
  expect(onPress).not.toHaveBeenCalled();
});

it("keeps history tappable and read actions enabled during workspace operations", async () => {
  const { onPress, onSelect } = setup(true);
  await userEvent.press(screen.getByRole("button", { name: "Open test chat" }));
  expect(onPress).toHaveBeenCalledTimes(1);
  fireEvent(screen.getByTestId("native-context-menu"), "longPress");
  const rename = screen.getByRole("button", { name: "Rename" });
  const remove = screen.getByRole("button", { name: "Delete" });
  expect(rename).toBeDisabled();
  expect(remove).toBeDisabled();
  await userEvent.press(rename);
  await userEvent.press(remove);
  expect(onSelect).not.toHaveBeenCalled();
  await userEvent.press(screen.getByRole("button", { name: "Mark as read" }));
  expect(onSelect).toHaveBeenCalledWith("read");
});
