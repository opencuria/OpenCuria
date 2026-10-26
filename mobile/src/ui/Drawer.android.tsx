import React, { useEffect, useRef } from "react";
import {
  BackHandler,
  DrawerLayoutAndroid,
  View,
  useWindowDimensions,
} from "react-native";
import {
  PanelLeft,
  Settings2,
  SquarePen,
  ChevronDown,
} from "lucide-react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { DrawerProps } from "./Drawer.types";
import { Button, IconButton } from "./controls";
import { NativeScreen } from "./NativeScreen";
import { useTheme } from "./theme";

/** DrawerLayout owns the edge gesture, scrim, animation and back handling. */
export function Drawer(props: DrawerProps): React.JSX.Element {
  const drawer = useRef<DrawerLayoutAndroid>(null);
  const { width } = useWindowDimensions();
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  useEffect(() => {
    if (props.visible) drawer.current?.openDrawer();
    else drawer.current?.closeDrawer();
  }, [props.visible]);
  useEffect(() => {
    if (!props.visible) return;
    const handler = BackHandler.addEventListener("hardwareBackPress", () => {
      props.onClose();
      return true;
    });
    return () => handler.remove();
  }, [props.visible, props.onClose]);
  return (
    <DrawerLayoutAndroid
      ref={drawer}
      style={{ flex: 1 }}
      drawerWidth={Math.min(360, width - 56)}
      drawerBackgroundColor={theme.surface}
      onDrawerOpen={props.onOpen}
      onDrawerClose={props.onClose}
      keyboardDismissMode="on-drag"
      renderNavigationView={() => (
        <View
          testID="conversation-drawer"
          style={{
            flex: 1,
            paddingTop: insets.top,
            paddingBottom: insets.bottom,
            backgroundColor: theme.surface,
          }}
        >
          {props.navigation}
          <Button
            title={props.profileTitle}
            onPress={props.onProfile}
            testID="drawer-profile"
            variant="ghost"
          />
        </View>
      )}
    >
      <NativeScreen
        title={props.title}
        left={
          <IconButton
            label="Open conversation drawer"
            icon={<PanelLeft color={theme.primary} size={24} />}
            onPress={props.onOpen}
            testID="main-menu"
          />
        }
        right={
          <View style={{ flexDirection: "row" }}>
            <IconButton
              label="Choose workspace"
              icon={<ChevronDown color={theme.primary} size={22} />}
              onPress={props.onWorkspaces}
            />
            <IconButton
              label="New chat"
              icon={<SquarePen color={theme.primary} size={22} />}
              onPress={props.onCreate}
              disabled={!props.canCreate}
              testID="new-chat"
            />
            <IconButton
              label="Profile and settings"
              icon={<Settings2 color={theme.primary} size={22} />}
              onPress={props.onProfile}
              testID="profile-menu"
            />
          </View>
        }
      >
        {props.children}
      </NativeScreen>
    </DrawerLayoutAndroid>
  );
}
