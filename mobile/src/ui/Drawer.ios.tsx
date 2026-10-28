import React, { useRef } from "react";
import { Platform, View } from "react-native";
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
} from "expo-router/react-navigation";
import {
  createNativeStackNavigator,
  type NativeStackHeaderItem,
} from "expo-router/native-stack";
import type { NativeSymbol } from "./controls.types";
import type { DrawerProps } from "./Drawer.types";
import { HeaderChromeInset, navigationTheme } from "./NativeScreen";
import { SlideDrawer } from "./SlideDrawer";
import { useTheme } from "./theme";

type Routes = { chat: undefined };
const Stack = createNativeStackNavigator<Routes>();
/** iOS 26 bar items float as liquid glass above content; older systems blur the bar. */
const LIQUID_GLASS = Number.parseInt(String(Platform.Version), 10) >= 26;

/**
 * iPhone: the sidebar slides in beneath the chat, whose transparent native bar
 * keeps its liquid-glass items. iPad keeps the adaptive split view.
 */
export function Drawer(props: DrawerProps): React.JSX.Element {
  return Platform.OS === "ios" && Platform.isPad ? (
    <SplitDrawer {...props} />
  ) : (
    <SlideDrawer
      open={props.visible}
      onOpen={props.onOpen}
      onClose={props.onClose}
      sidebar={props.navigation}
    >
      <ChatStack {...props} />
    </SlideDrawer>
  );
}

/** A single native-stack screen supplies the transparent bar and scroll edge. */
function ChatStack(props: DrawerProps): React.JSX.Element {
  const { theme } = useTheme();
  const latest = useRef(props);
  latest.current = props;

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
      <NavigationContainer theme={navigationTheme(theme)}>
        <Stack.Navigator
          screenOptions={{
            headerShadowVisible: false,
            headerTintColor: theme.text,
            contentStyle: { backgroundColor: theme.background },
          }}
        >
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
                  label: "Open sidebar",
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
              navigationTitle(""),
              navigationBarTitleDisplayMode("inline"),
            ]}
          >
            <RNHostView>
              <View testID="conversation-drawer" style={{ flex: 1 }}>
                {props.navigation}
              </View>
            </RNHostView>
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
                  label="Open sidebar"
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
