import React, { useCallback, useEffect, useRef } from "react";
import { Platform, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  Button as SwiftButton,
  Host,
  Menu,
  NavigationSplitView,
  RNHostView,
  Toolbar,
  ToolbarItem,
} from "@expo/ui/swift-ui";
import {
  disabled,
  labelStyle,
  navigationBarTitleDisplayMode,
  navigationTitle,
} from "@expo/ui/swift-ui/modifiers";
import {
  NavigationContainer,
  NavigationIndependentTree,
  StackActions,
  useNavigationContainerRef,
} from "expo-router/react-navigation";
import {
  createNativeStackNavigator,
  type NativeStackHeaderItem,
} from "expo-router/native-stack";
import { Button } from "./controls";
import type { NativeSymbol } from "./controls.types";
import type { DrawerProps } from "./Drawer.types";
import { HeaderChromeInset, navigationTheme } from "./NativeScreen";
import { useTheme } from "./theme";

type Routes = { chats: undefined; chat: undefined };
const Stack = createNativeStackNavigator<Routes>();
/** iOS 26 bar items float as liquid glass above content; older systems blur the bar. */
const LIQUID_GLASS = Number.parseInt(String(Platform.Version), 10) >= 26;

/**
 * iPhone: a native stack whose root is the chat list and whose pushed chat
 * scrolls beneath a transparent bar. iPad keeps the adaptive split view.
 */
export function Drawer(props: DrawerProps): React.JSX.Element {
  return Platform.OS === "ios" && Platform.isPad ? (
    <SplitDrawer {...props} />
  ) : (
    <StackDrawer {...props} />
  );
}

function StackDrawer(props: DrawerProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const navigation = useNavigationContainerRef<Routes>();
  const latest = useRef(props);
  latest.current = props;
  // Open straight into the chat without a push animation on first render.
  const initialState = useRef(
    props.visible
      ? undefined
      : {
          index: 1,
          routes: [{ name: "chats" as const }, { name: "chat" as const }],
        },
  ).current;

  const sync = useCallback(() => {
    if (!navigation.isReady()) return;
    const top = navigation.getCurrentRoute()?.name;
    if (props.visible && top === "chat")
      navigation.dispatch(StackActions.popToTop());
    else if (!props.visible && top === "chats")
      navigation.dispatch(StackActions.push("chat"));
  }, [navigation, props.visible]);
  useEffect(sync, [sync]);

  const rightItems = (): NativeStackHeaderItem[] => [
    {
      type: "button",
      label: "New chat",
      icon: { type: "sfSymbol", name: "plus.bubble" },
      disabled: !props.canCreate,
      onPress: () => latest.current.onCreate(),
    },
    {
      type: "menu",
      label: "More",
      icon: { type: "sfSymbol", name: "ellipsis" },
      menu: {
        title: props.title,
        items: props.menuActions.map((action) => ({
          type: "action" as const,
          label: action.title,
          icon: action.symbol
            ? { type: "sfSymbol" as const, name: action.symbol as NativeSymbol }
            : undefined,
          destructive: action.destructive,
          disabled: action.disabled,
          onPress: () => latest.current.onMenuAction(action.id),
        })),
      },
    },
  ];

  return (
    <NavigationIndependentTree>
      <NavigationContainer
        ref={navigation}
        theme={navigationTheme(theme)}
        initialState={initialState}
        onReady={sync}
        onStateChange={(state) => {
          // Reflect native back swipes in the owner's drawer state.
          const top = state?.routes[state.index]?.name;
          if (top === "chats" && !latest.current.visible)
            latest.current.onOpen();
          else if (top === "chat" && latest.current.visible)
            latest.current.onClose();
        }}
      >
        <Stack.Navigator
          screenOptions={{
            headerShadowVisible: false,
            headerTintColor: theme.text,
            contentStyle: { backgroundColor: theme.background },
          }}
        >
          <Stack.Screen name="chats" options={{ title: "OpenCuria" }}>
            {() => (
              <View
                testID="conversation-drawer"
                style={{ flex: 1, paddingBottom: insets.bottom }}
              >
                {props.navigation}
                <Button
                  title={props.profileTitle}
                  symbol="person.crop.circle"
                  variant="ghost"
                  onPress={props.onProfile}
                  testID="drawer-profile"
                />
              </View>
            )}
          </Stack.Screen>
          <Stack.Screen
            name="chat"
            options={{
              title: "",
              headerBackVisible: false,
              headerTransparent: true,
              headerBlurEffect: LIQUID_GLASS
                ? undefined
                : "systemChromeMaterial",
              scrollEdgeEffects: { top: "soft" },
              unstable_headerLeftItems: () => [
                {
                  type: "button",
                  label: "Open chats",
                  icon: { type: "sfSymbol", name: "line.3.horizontal" },
                  onPress: () => latest.current.onOpen(),
                },
              ],
              unstable_headerRightItems: rightItems,
            }}
          >
            {() => <HeaderChromeInset>{props.children}</HeaderChromeInset>}
          </Stack.Screen>
        </Stack.Navigator>
      </NavigationContainer>
    </NavigationIndependentTree>
  );
}

/** UIKit manages adaptive sidebar columns and gestures on iPad. */
function SplitDrawer(props: DrawerProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      style={{ flex: 1 }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
    >
      <NavigationSplitView
        columnVisibility={props.visible ? "all" : "detailOnly"}
        preferredCompactColumn={props.visible ? "sidebar" : "detail"}
        onColumnVisibilityChange={(visibility) =>
          visibility === "detailOnly" ? props.onClose() : props.onOpen()
        }
        onPreferredCompactColumnChange={(column) =>
          column === "sidebar" ? props.onOpen() : props.onClose()
        }
      >
        <NavigationSplitView.Sidebar>
          <Toolbar
            modifiers={[
              navigationTitle("OpenCuria"),
              navigationBarTitleDisplayMode("inline"),
            ]}
          >
            <RNHostView>
              <View testID="conversation-drawer" style={{ flex: 1 }}>
                {props.navigation}
              </View>
            </RNHostView>
            <Toolbar.Content>
              <ToolbarItem placement="bottomBar">
                <SwiftButton
                  label={props.profileTitle}
                  systemImage="person.crop.circle"
                  onPress={props.onProfile}
                  testID="drawer-profile"
                />
              </ToolbarItem>
            </Toolbar.Content>
          </Toolbar>
        </NavigationSplitView.Sidebar>
        <NavigationSplitView.Detail>
          <Toolbar
            modifiers={[
              navigationTitle(""),
              navigationBarTitleDisplayMode("inline"),
            ]}
          >
            <RNHostView>
              <View style={{ flex: 1 }}>{props.children}</View>
            </RNHostView>
            <Toolbar.Content>
              <ToolbarItem placement="topBarLeading">
                <SwiftButton
                  label="Open chats"
                  systemImage="sidebar.left"
                  onPress={props.onOpen}
                  testID="main-menu"
                  modifiers={[labelStyle("iconOnly")]}
                />
              </ToolbarItem>
              <ToolbarItem placement="topBarTrailing">
                <SwiftButton
                  label="New chat"
                  systemImage="plus.bubble"
                  onPress={props.onCreate}
                  testID="new-chat"
                  modifiers={[
                    labelStyle("iconOnly"),
                    disabled(!props.canCreate),
                  ]}
                />
              </ToolbarItem>
              <ToolbarItem placement="topBarTrailing">
                <Menu
                  label="More"
                  systemImage="ellipsis"
                  modifiers={[labelStyle("iconOnly")]}
                >
                  {props.menuActions.map((action) => (
                    <SwiftButton
                      key={action.id}
                      label={action.title}
                      systemImage={action.symbol as NativeSymbol | undefined}
                      role={action.destructive ? "destructive" : undefined}
                      modifiers={[disabled(Boolean(action.disabled))]}
                      onPress={() => props.onMenuAction(action.id)}
                    />
                  ))}
                </Menu>
              </ToolbarItem>
            </Toolbar.Content>
          </Toolbar>
        </NavigationSplitView.Detail>
      </NavigationSplitView>
    </Host>
  );
}
