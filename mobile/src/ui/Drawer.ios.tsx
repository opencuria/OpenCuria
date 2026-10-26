import React from "react";
import { View } from "react-native";
import {
  Button,
  Host,
  NavigationSplitView,
  RNHostView,
  Toolbar,
  ToolbarItem,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  disabled,
  labelStyle,
  navigationBarTitleDisplayMode,
  navigationTitle,
} from "@expo/ui/swift-ui/modifiers";
import type { DrawerProps } from "./Drawer.types";
import { useTheme } from "./theme";

/** UIKit manages adaptive sidebar columns, compact navigation and gestures. */
export function Drawer(props: DrawerProps): React.JSX.Element {
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
                <Button
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
              navigationTitle(props.title),
              navigationBarTitleDisplayMode("inline"),
            ]}
          >
            <RNHostView>
              <View style={{ flex: 1 }}>{props.children}</View>
            </RNHostView>
            <Toolbar.Content>
              <ToolbarItem placement="topBarLeading">
                <Button
                  label="Open conversation drawer"
                  systemImage="sidebar.left"
                  onPress={props.onOpen}
                  testID="main-menu"
                  modifiers={[labelStyle("iconOnly")]}
                />
              </ToolbarItem>
              <ToolbarItem placement="principal">
                <Button
                  label={props.title}
                  onPress={props.onWorkspaces}
                  modifiers={[accessibilityLabel(`Workspace: ${props.title}`)]}
                />
              </ToolbarItem>
              <ToolbarItem placement="topBarTrailing">
                <Button
                  label="New chat"
                  systemImage="square.and.pencil"
                  onPress={props.onCreate}
                  testID="new-chat"
                  modifiers={[
                    labelStyle("iconOnly"),
                    disabled(!props.canCreate),
                  ]}
                />
              </ToolbarItem>
              <ToolbarItem placement="topBarTrailing">
                <Button
                  label="Profile and settings"
                  systemImage="person.crop.circle"
                  onPress={props.onProfile}
                  testID="profile-menu"
                  modifiers={[labelStyle("iconOnly")]}
                />
              </ToolbarItem>
            </Toolbar.Content>
          </Toolbar>
        </NavigationSplitView.Detail>
      </NavigationSplitView>
    </Host>
  );
}
