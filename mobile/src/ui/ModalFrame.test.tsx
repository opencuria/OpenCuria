import React from "react";
import { Text } from "react-native";
import {
  render,
  screen,
  userEvent,
  within,
} from "@testing-library/react-native";
import { ModalFrame } from "./ModalFrame.ios";
import type { ModalFrameProps } from "./ModalFrame.types";
import { ThemeProvider } from "./theme";

jest.unmock("./ModalFrame.ios");
// The confirmation's label comes from the real prominent-button label.
jest.unmock("./controls.ios");
jest.mock("expo-glass-effect", () => ({ isLiquidGlassAvailable: () => true }));

interface Modifier {
  name: string;
  args: unknown[];
}

// Every modifier records its name and arguments.
jest.mock(
  "@expo/ui/swift-ui/modifiers",
  () =>
    new Proxy(
      {},
      {
        get: (_target, name) =>
          name === "__esModule"
            ? true
            : (...args: unknown[]) => ({ name, args }),
      },
    ),
);

// Keep ModalFrame real and mock only the SwiftUI views.
jest.mock("@expo/ui/swift-ui", () => {
  const { Pressable, Text: RNText, View } = require("react-native");
  const Container = ({ children }: React.PropsWithChildren) => (
    <View>{children}</View>
  );
  const Toolbar = ({
    children,
    modifiers,
  }: React.PropsWithChildren<{ modifiers: Modifier[] }>) => (
    <View testID="toolbar" modifiers={modifiers}>
      {children}
    </View>
  );
  Toolbar.Content = Container;
  return {
    Host: Container,
    NavigationStack: Container,
    RNHostView: Container,
    VStack: Container,
    HStack: Container,
    Menu: Container,
    Toolbar,
    ToolbarItem: ({
      placement,
      children,
    }: React.PropsWithChildren<{ placement: string }>) => (
      <View testID={`toolbar-${placement}`}>{children}</View>
    ),
    ProgressView: () => <RNText>Progress</RNText>,
    Image: () => null,
    Text: ({
      children,
      modifiers,
    }: React.PropsWithChildren<{ modifiers: Modifier[] }>) => (
      <RNText modifiers={modifiers}>{children}</RNText>
    ),
    // Like SwiftUI below iOS 26, a button without a label has no name.
    Button: ({
      label,
      role,
      systemImage,
      onPress,
      modifiers = [],
      children,
    }: React.PropsWithChildren<{
      label?: string;
      role?: string;
      systemImage?: string;
      onPress?: () => void;
      modifiers?: Modifier[];
    }>) => {
      const named = modifiers.find((m) => m.name === "accessibilityLabel");
      return (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={label ?? (named ? String(named.args[0]) : "")}
          disabled={modifiers.some((m) => m.name === "disabled" && m.args[0])}
          onPress={onPress}
          // `role` is React Native's accessibility role; record it apart.
          buttonRole={role}
          systemImage={systemImage}
          modifiers={modifiers}
        >
          {children}
        </Pressable>
      );
    },
    useNativeState: () => ({ set: () => undefined }),
  };
});

function setup(props: Partial<ModalFrameProps> = {}) {
  const onClose = jest.fn();
  render(
    <ThemeProvider>
      <ModalFrame title="Workspaces" onClose={onClose} {...props}>
        <Text>Content</Text>
      </ModalFrame>
    </ThemeProvider>,
  );
  return { onClose };
}

function titleMode() {
  const modifiers: Modifier[] = screen.getByTestId("toolbar").props.modifiers;
  return modifiers.find((m) => m.name === "navigationBarTitleDisplayMode")
    ?.args[0];
}

function labelColor(title: string) {
  const modifiers: Modifier[] = screen.getByText(title).props.modifiers;
  return modifiers.find((m) => m.name === "foregroundStyle")?.args[0];
}

it("draws its own xmark so close stays visible below iOS 26", () => {
  setup();
  const close = within(screen.getByTestId("toolbar-topBarLeading")).getByRole(
    "button",
    { name: "Close" },
  );
  expect(close.props.buttonRole).toBe("close");
  expect(close.props.systemImage).toBe("xmark");
  const modifiers: Modifier[] = close.props.modifiers;
  expect(modifiers).toContainEqual({ name: "labelStyle", args: ["iconOnly"] });
});

it("renders a form inside the navigation stack instead of its children", () => {
  setup({
    largeTitle: true,
    form: {
      testID: "workspaces-form",
      sections: [
        {
          id: "all",
          rows: [{ kind: "item", id: "alpha", title: "Alpha" }],
        },
      ],
    },
  });
  expect(screen.getByTestId("workspaces-form")).toBeTruthy();
  expect(screen.getByText("Alpha")).toBeTruthy();
  expect(screen.queryByText("Content")).toBeNull();
  expect(titleMode()).toBe("large");
});

it("shows a form's empty state with its action", async () => {
  const onPress = jest.fn();
  setup({
    form: {
      sections: [],
      empty: {
        title: "No workspaces yet",
        symbol: "square.stack.3d.up",
        action: { title: "New workspace", onPress },
      },
    },
  });
  expect(screen.getByText("No workspaces yet")).toBeTruthy();
  await userEvent.press(screen.getByRole("button", { name: "New workspace" }));
  expect(onPress).toHaveBeenCalledTimes(1);
});

it("closes from the leading edge, also next to a confirmation", async () => {
  const onPress = jest.fn();
  const { onClose } = setup({ primaryAction: { title: "Create", onPress } });
  const leading = screen.getByTestId("toolbar-topBarLeading");
  await userEvent.press(within(leading).getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(
    within(screen.getByTestId("toolbar-confirmationAction")).getByRole(
      "button",
      { name: "Create" },
    ),
  ).toBeTruthy();
  expect(titleMode()).toBe("inline");
});

it("shows a large title for collection sheets", () => {
  setup({ largeTitle: true });
  expect(titleMode()).toBe("large");
  expect(
    within(screen.getByTestId("toolbar-topBarLeading")).getByRole("button", {
      name: "Close",
    }),
  ).toBeTruthy();
});

it("places an icon action as the primary action", async () => {
  const onPress = jest.fn();
  setup({
    largeTitle: true,
    primaryAction: { title: "New workspace", symbol: "plus", onPress },
  });
  await userEvent.press(
    within(screen.getByTestId("toolbar-primaryAction")).getByRole("button", {
      name: "New workspace",
    }),
  );
  expect(onPress).toHaveBeenCalledTimes(1);
});

it("draws an enabled confirmation label in the label-on-ink color", () => {
  setup({ primaryAction: { title: "Save", onPress: jest.fn() } });
  // Light mode: white on the near-black ink.
  expect(labelColor("Save")).toBe("#FFFFFF");
});

it("leaves a disabled confirmation to the system's dimming", () => {
  setup({
    primaryAction: { title: "Save", onPress: jest.fn(), disabled: true },
  });
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(labelColor("Save")).toBeUndefined();
});

it("shows progress in place of a busy action", () => {
  setup({ primaryAction: { title: "Save", onPress: jest.fn(), busy: true } });
  expect(
    within(screen.getByTestId("toolbar-confirmationAction")).getByText(
      "Progress",
    ),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
});
