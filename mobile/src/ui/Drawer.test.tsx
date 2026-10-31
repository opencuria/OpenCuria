import React from "react";
import { Platform, Text } from "react-native";
import { render } from "@testing-library/react-native";
import type { NativeStackHeaderItem } from "expo-router/native-stack";
import { Drawer } from "./Drawer.ios";
import type { DrawerProps } from "./Drawer.types";
import { ThemeProvider } from "./theme";

jest.unmock("./Drawer.ios");

const mockScreen = jest.fn();

jest.mock("expo-router/native-stack", () => ({
  createNativeStackNavigator: () => ({
    Navigator: ({ children }: React.PropsWithChildren) => children,
    Screen: (props: { children: () => React.ReactNode }) => {
      mockScreen(props);
      return props.children();
    },
  }),
}));
jest.mock("expo-router/react-navigation", () => ({
  NavigationContainer: ({ children }: React.PropsWithChildren) => children,
  NavigationIndependentTree: ({ children }: React.PropsWithChildren) =>
    children,
}));
jest.mock("@expo/ui/swift-ui", () => ({}));
jest.mock("./SlideDrawer", () => ({
  SlideDrawer: ({ children }: React.PropsWithChildren) => children,
}));
jest.mock("./NativeScreen", () => ({
  HeaderChromeInset: ({ children }: React.PropsWithChildren) => children,
  navigationTheme: () => ({}),
}));

function moreMenu() {
  const { options } = mockScreen.mock.calls.at(-1)![0];
  return (options.unstable_headerRightItems() as NativeStackHeaderItem[]).find(
    (item) => item.type === "menu",
  )!;
}

function setup() {
  const props: DrawerProps = {
    visible: false,
    onOpen: jest.fn(),
    onClose: jest.fn(),
    onCreate: jest.fn(),
    canCreate: true,
    title: "Workspace",
    navigation: <Text>Sidebar</Text>,
    children: <Text>Chat</Text>,
    menuActions: [
      { id: "workspace", title: "Switch workspace", symbol: "square.grid.2x2" },
      { id: "rename", title: "Rename", disabled: true },
      { id: "delete", title: "Delete", destructive: true },
    ],
    onMenuAction: jest.fn(),
  };
  const tree = (value: DrawerProps) => (
    <ThemeProvider>
      <Drawer {...value} />
    </ThemeProvider>
  );
  const result = render(tree(props));
  return {
    props,
    rerender: (value: DrawerProps) => result.rerender(tree(value)),
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.replaceProperty(Platform, "OS", "ios");
  jest.spyOn(Platform, "isPad", "get").mockReturnValue(false);
});

afterEach(() => jest.restoreAllMocks());

it("keeps chat commands out of UIKit's automatic selection mode", () => {
  setup();
  const more = moreMenu();
  expect(more.type).toBe("menu");
  if (more.type !== "menu") throw new Error("Expected overflow menu");
  expect(more.changesSelectionAsPrimaryAction).toBe(false);
  // Expo Router maps this to UIMenu's singleSelection: false. Omitting it
  // adds a checkmark after choosing a command even without action state.
  expect(more.menu.multiselectable).toBe(true);
  for (const action of more.menu.items)
    expect(action).not.toHaveProperty("state");
});

it("dispatches commands using the latest chat props without selecting them", () => {
  const { props, rerender } = setup();
  const more = moreMenu();
  if (more.type !== "menu") throw new Error("Expected overflow menu");
  const workspace = more.menu.items[0];
  if (workspace.type !== "action") throw new Error("Expected command");
  const nextOnMenuAction = jest.fn();
  rerender({ ...props, onMenuAction: nextOnMenuAction });
  workspace.onPress();
  expect(props.onMenuAction).not.toHaveBeenCalled();
  expect(nextOnMenuAction).toHaveBeenCalledWith("workspace");
  expect(workspace).not.toHaveProperty("state");
});

it("preserves native disabled and destructive command behavior", () => {
  setup();
  const more = moreMenu();
  if (more.type !== "menu") throw new Error("Expected overflow menu");
  expect(more.menu.items[1]).toMatchObject({ label: "Rename", disabled: true });
  expect(more.menu.items[2]).toMatchObject({
    label: "Delete",
    destructive: true,
  });
});
