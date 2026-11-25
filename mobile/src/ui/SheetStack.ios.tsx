import React, { useCallback, useEffect, useMemo, useRef } from "react";
import { Platform, View, useWindowDimensions } from "react-native";
import {
  HeaderShownContext,
  NavigationContainer,
  NavigationIndependentTree,
  createNavigationContainerRef,
  useHeaderHeight,
} from "expo-router/react-navigation";
import {
  createNativeStackNavigator,
  type NativeStackHeaderItem,
  type NativeStackHeaderItemMenuAction,
  type NativeStackNavigationOptions,
} from "expo-router/native-stack";
import { ChromeInsetContext } from "./chromeInset";
import type { NativeSymbol } from "./controls.types";
import type { NativeMenuAction } from "./NativeMenu";
import { navigationTheme } from "./NativeScreen";
import { NavigationTitle } from "./NavigationTitle";
import type {
  SheetPage,
  SheetPageAction,
  SheetStackProps,
} from "./SheetStack.types";
import { useTheme } from "./theme";

type Routes = { root: undefined; detail: undefined };
type PageKey = keyof Routes;
const Stack = createNativeStackNavigator<Routes>();
/** iOS 26 bar items float as liquid glass above content; older systems blur the bar. */
const LIQUID_GLASS = Number.parseInt(String(Platform.Version), 10) >= 26;
/** UIKit's bar margin, one bar item, and the space between item groups. */
const BAR_MARGIN = 16;
const ITEM_WIDTH = 44;
const ITEM_GAP = 8;

/** Room the trailing items take: one capsule, plus one per standalone item. */
function trailingWidth(page: SheetPage): number {
  const actions = page.actions ?? [];
  const items = actions.length + (page.menu ? 1 : 0);
  if (!items) return 0;
  const standalone = actions.filter((action) => action.standalone).length;
  const groups = standalone + (items > standalone ? 1 : 0);
  return items * ITEM_WIDTH + (groups - 1) * ITEM_GAP;
}

/** Widest a centered title may be without reaching the bar items. */
function titleWidth(width: number, page: SheetPage, back: boolean): number {
  const side = Math.max(back ? ITEM_WIDTH : 0, trailingWidth(page));
  return Math.max(0, width - 2 * (BAR_MARGIN + side + ITEM_GAP));
}

function menuAction(
  action: NativeMenuAction,
  onSelect: (id: string) => void,
): NativeStackHeaderItemMenuAction {
  return {
    type: "action",
    label: action.title,
    description: action.subtitle,
    icon: action.symbol
      ? { type: "sfSymbol", name: action.symbol as NativeSymbol }
      : undefined,
    destructive: action.destructive,
    disabled: action.disabled,
    // Commands carry no state; toggles show UIKit's checkmark.
    ...(action.selected === undefined
      ? {}
      : { state: action.selected ? ("on" as const) : ("off" as const) }),
    onPress: () => onSelect(action.id),
  };
}

/**
 * The pages of a sheet in a UIKit navigation controller: a transparent bar
 * with the chat's scroll edge, glass bar items and the system's push, back
 * button and edge swipe. The bar sits below the sheet's grabber, never
 * beneath the status bar.
 */
export function SheetStack(props: SheetStackProps): React.JSX.Element {
  const { theme } = useTheme();
  const { width } = useWindowDimensions();
  const latest = useRef(props);
  latest.current = props;
  const navigation = useMemo(() => createNavigationContainerRef<Routes>(), []);
  const hasDetail = Boolean(props.detail);
  const showDetail = props.detailShown && hasDetail;

  // The stack follows `detailShown`; going back natively reports through
  // `onStateChange`, so both directions meet in the same state.
  const sync = useCallback(() => {
    if (!navigation.isReady()) return;
    const top = navigation.getCurrentRoute()?.name;
    if (showDetail && top !== "detail") navigation.navigate("detail");
    else if (!showDetail && top === "detail") navigation.goBack();
  }, [navigation, showDetail]);
  useEffect(sync, [sync]);
  const initialState = useRef({
    index: showDetail ? 1 : 0,
    routes: showDetail
      ? [{ name: "root" as const }, { name: "detail" as const }]
      : [{ name: "root" as const }],
  }).current;

  /** Reads the page's latest props, so items never call a stale handler. */
  const pageOf = (key: PageKey): SheetPage | undefined =>
    key === "root" ? latest.current.root : latest.current.detail;
  const actionOf = (key: PageKey, id: string): SheetPageAction | undefined =>
    pageOf(key)?.actions?.find((action) => action.id === id);

  const rightItems = (
    key: PageKey,
    page: SheetPage,
  ): NativeStackHeaderItem[] => [
    ...(page.actions ?? []).map(
      (action): NativeStackHeaderItem => ({
        type: "button",
        label: action.label,
        icon: { type: "sfSymbol", name: action.symbol },
        tintColor: action.tint,
        disabled: action.disabled,
        accessibilityLabel: action.label,
        accessibilityHint: action.accessibilityHint,
        ...(action.standalone ? { sharesBackground: false } : {}),
        onPress: () => actionOf(key, action.id)?.onPress(),
      }),
    ),
    ...(page.menu?.sections.some((section) => section.actions.length)
      ? [
          {
            type: "menu" as const,
            label: page.menu.label,
            icon: { type: "sfSymbol" as const, name: "ellipsis" as const },
            changesSelectionAsPrimaryAction: false,
            menu: {
              // Native-stack otherwise makes these single-selection menus, so
              // UIKit checks the last command the user ran.
              multiselectable: true,
              items: page.menu.sections
                .filter((section) => section.actions.length)
                .map((section) => ({
                  type: "submenu" as const,
                  label: section.title ?? "",
                  inline: true,
                  multiselectable: true,
                  items: section.actions.map((action) =>
                    menuAction(action, (id) => pageOf(key)?.menu?.onSelect(id)),
                  ),
                })),
            },
          },
        ]
      : []),
  ];

  const options = (
    key: PageKey,
    page: SheetPage,
  ): NativeStackNavigationOptions => {
    const back = key === "detail";
    return {
      title: page.title,
      headerTitle: page.subtitle
        ? () => (
            <NavigationTitle
              title={page.title}
              subtitle={page.subtitle}
              maxWidth={titleWidth(width, page, back)}
              testID={page.testID ? `${page.testID}-title` : undefined}
            />
          )
        : undefined,
      headerBackVisible: back,
      headerBackButtonDisplayMode: "minimal",
      headerTransparent: true,
      headerBlurEffect: LIQUID_GLASS ? undefined : "systemChromeMaterial",
      scrollEdgeEffects: { top: "soft" },
      unstable_headerRightItems: () => rightItems(key, page),
    };
  };

  const reportBarHeight = useCallback(
    (height: number) => latest.current.onBarHeight?.(height),
    [],
  );
  const scene = (page: SheetPage) => (
    <SheetScene testID={page.testID} onBarHeight={reportBarHeight}>
      {page.children}
    </SheetScene>
  );

  return (
    <NavigationIndependentTree>
      <NavigationContainer
        ref={navigation}
        theme={navigationTheme(theme)}
        initialState={initialState}
        onReady={sync}
        onStateChange={(state) => {
          const shown = state?.routes[state.index]?.name === "detail";
          if (shown !== latest.current.detailShown)
            latest.current.onDetailShownChange(shown);
        }}
      >
        {/* The sheet's grabber strip, not the status bar, lies above the bar. */}
        <HeaderShownContext.Provider value>
          <Stack.Navigator
            screenOptions={{
              headerShadowVisible: false,
              headerTintColor: theme.text,
              contentStyle: { backgroundColor: theme.background },
            }}
          >
            <Stack.Screen name="root" options={options("root", props.root)}>
              {() => scene(props.root)}
            </Stack.Screen>
            {props.detail ? (
              <Stack.Screen
                name="detail"
                options={options("detail", props.detail)}
              >
                {() => (props.detail ? scene(props.detail) : null)}
              </Stack.Screen>
            ) : null}
          </Stack.Navigator>
        </HeaderShownContext.Provider>
      </NavigationContainer>
    </NavigationIndependentTree>
  );
}

/** Publishes the measured bar to content scrolling beneath it. */
function SheetScene({
  children,
  onBarHeight,
  testID,
}: React.PropsWithChildren<{
  onBarHeight: (height: number) => void;
  testID?: string;
}>): React.JSX.Element {
  const height = useHeaderHeight();
  useEffect(() => onBarHeight(height), [height, onBarHeight]);
  return (
    <ChromeInsetContext.Provider value={height}>
      <View style={{ flex: 1 }} testID={testID}>
        {children}
      </View>
    </ChromeInsetContext.Provider>
  );
}
