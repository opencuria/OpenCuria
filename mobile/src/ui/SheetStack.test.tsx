import React from "react";
import { Text } from "react-native";
import { render, screen } from "@testing-library/react-native";
import type {
  NativeStackHeaderItem,
  NativeStackNavigationOptions,
} from "expo-router/native-stack";
import { SheetStack } from "./SheetStack.ios";
import type { SheetPage, SheetStackProps } from "./SheetStack.types";
import { ThemeProvider } from "./theme";

jest.unmock("./SheetStack.ios");

const mockScreens = new Map<
  string,
  { options: NativeStackNavigationOptions }
>();
const mockContainer: { props?: Record<string, unknown> } = {};

jest.mock("expo-router/native-stack", () => ({
  createNativeStackNavigator: () => ({
    Navigator: ({ children }: React.PropsWithChildren) => children,
    Screen: (props: {
      name: string;
      options: NativeStackNavigationOptions;
      children: () => React.ReactNode;
    }) => {
      mockScreens.set(props.name, props);
      return props.children();
    },
  }),
}));
jest.mock("expo-router/react-navigation", () => {
  const { createContext } = jest.requireActual<typeof React>("react");
  return {
    HeaderShownContext: createContext(false),
    NavigationContainer: ({
      children,
      ...props
    }: React.PropsWithChildren<Record<string, unknown>>) => {
      mockContainer.props = props;
      return children;
    },
    NavigationIndependentTree: ({ children }: React.PropsWithChildren) =>
      children,
    createNavigationContainerRef: () => ({ isReady: () => false }),
    useHeaderHeight: () => 52,
  };
});
jest.mock("./NativeScreen", () => ({ navigationTheme: () => ({}) }));

const RED = "#ff3b30";

function pages(onRecord = jest.fn(), onSelect = jest.fn()) {
  const root: SheetPage = {
    title: "Chats",
    subtitle: "Demo",
    actions: [
      {
        id: "new",
        label: "New chat",
        symbol: "square.and.pencil",
        icon: () => null,
        onPress: jest.fn(),
      },
    ],
    children: <Text>List</Text>,
  };
  const detail: SheetPage = {
    title: "Make the button green",
    subtitle: "Todo Demo · Demo",
    actions: [
      {
        id: "record",
        label: "Record",
        symbol: "record.circle",
        icon: () => null,
        tint: RED,
        standalone: true,
        onPress: onRecord,
      },
    ],
    menu: {
      label: "More",
      onSelect,
      sections: [
        {
          id: "chat",
          actions: [{ id: "rename", title: "Rename chat", symbol: "pencil" }],
        },
        { id: "empty", actions: [] },
        {
          id: "triggers",
          actions: [
            { id: "shake", title: "Open by shaking", selected: true },
            { id: "button", title: "Show the menu button", selected: false },
          ],
        },
      ],
    },
    children: <Text>Chat</Text>,
  };
  return { root, detail };
}

function setup(overrides: Partial<SheetStackProps> = {}) {
  const props: SheetStackProps = {
    ...pages(),
    detailShown: true,
    onDetailShownChange: jest.fn(),
    onBarHeight: jest.fn(),
    ...overrides,
  };
  const tree = (value: SheetStackProps) => (
    <ThemeProvider>
      <SheetStack {...value} />
    </ThemeProvider>
  );
  const result = render(tree(props));
  return {
    props,
    rerender: (value: SheetStackProps) => result.rerender(tree(value)),
  };
}

function rightItems(name: string): NativeStackHeaderItem[] {
  const { options } = mockScreens.get(name)!;
  return options.unstable_headerRightItems!({ canGoBack: true });
}

beforeEach(() => {
  mockScreens.clear();
  mockContainer.props = undefined;
});

it("draws transparent bars with the chat's scroll edge and a back chevron on the detail", () => {
  setup();
  const root = mockScreens.get("root")!.options;
  const detail = mockScreens.get("detail")!.options;
  for (const options of [root, detail]) {
    expect(options).toMatchObject({
      headerTransparent: true,
      scrollEdgeEffects: { top: "soft" },
    });
  }
  expect(root.headerBackVisible).toBe(false);
  expect(detail).toMatchObject({
    headerBackVisible: true,
    headerBackButtonDisplayMode: "minimal",
  });
  // Both pages stay mounted, the chat list beneath the chat.
  expect(mockContainer.props?.initialState).toEqual({
    index: 1,
    routes: [{ name: "root" }, { name: "detail" }],
  });
});

it("puts a tinted action in its own glass item and calls its latest handler", () => {
  const first = jest.fn();
  const { props, rerender } = setup(pages(first));
  const [record] = rightItems("detail");
  expect(record).toMatchObject({
    type: "button",
    label: "Record",
    icon: { type: "sfSymbol", name: "record.circle" },
    tintColor: RED,
    sharesBackground: false,
  });
  const latest = jest.fn();
  rerender({ ...props, ...pages(latest) });
  if (record.type !== "button") throw new Error("Expected a button");
  record.onPress();
  expect(first).not.toHaveBeenCalled();
  expect(latest).toHaveBeenCalledTimes(1);
});

it("groups the overflow menu into sections; only toggles carry a checkmark", () => {
  const onSelect = jest.fn();
  setup(pages(jest.fn(), onSelect));
  const more = rightItems("detail").find((item) => item.type === "menu");
  if (more?.type !== "menu") throw new Error("Expected the overflow menu");
  expect(more.changesSelectionAsPrimaryAction).toBe(false);
  expect(more.menu.multiselectable).toBe(true);
  // The empty group is left out.
  expect(more.menu.items).toHaveLength(2);
  const [chat, triggers] = more.menu.items;
  if (chat.type !== "submenu" || triggers.type !== "submenu")
    throw new Error("Expected sections");
  expect(chat).toMatchObject({ inline: true, multiselectable: true });
  const [rename] = chat.items;
  expect(rename).not.toHaveProperty("state");
  expect(triggers.items.map((item) => "state" in item && item.state)).toEqual([
    "on",
    "off",
  ]);
  if (rename.type !== "action") throw new Error("Expected a command");
  rename.onPress();
  expect(onSelect).toHaveBeenCalledWith("rename");
});

it("reports going back natively, but not the push it asked for", () => {
  const { props } = setup();
  const onStateChange = mockContainer.props?.onStateChange as (
    state: unknown,
  ) => void;
  onStateChange({ index: 1, routes: [{ name: "root" }, { name: "detail" }] });
  expect(props.onDetailShownChange).not.toHaveBeenCalled();
  onStateChange({ index: 0, routes: [{ name: "root" }] });
  expect(props.onDetailShownChange).toHaveBeenCalledWith(false);
});

it("publishes the measured bar to the content and the sheet", () => {
  const { props } = setup({ detailShown: false });
  expect(props.onBarHeight).toHaveBeenCalledWith(52);
  expect(screen.getByText("List")).toBeTruthy();
  expect(mockContainer.props?.initialState).toEqual({
    index: 0,
    routes: [{ name: "root" }],
  });
});
