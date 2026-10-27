import React, { useEffect, useRef } from "react";
import {
  BackHandler,
  DrawerLayoutAndroid,
  StyleSheet,
  View,
  useWindowDimensions,
} from "react-native";
import { Ellipsis, Menu, MessageCirclePlus } from "lucide-react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { DrawerProps } from "./Drawer.types";
import { Button, IconButton } from "./controls";
import { NativeMenu } from "./NativeMenu";
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
  const chrome = [styles.chrome, { backgroundColor: theme.surfaceMuted }];
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
        title=""
        transparentHeader
        left={
          <View style={chrome}>
            <IconButton
              label="Open chats"
              icon={<Menu color={theme.text} size={22} />}
              onPress={props.onOpen}
              testID="main-menu"
            />
          </View>
        }
        right={
          <View style={[chrome, styles.group]}>
            <IconButton
              label="New chat"
              icon={<MessageCirclePlus color={theme.text} size={22} />}
              onPress={props.onCreate}
              disabled={!props.canCreate}
              testID="new-chat"
            />
            <NativeMenu
              label="More"
              actions={props.menuActions}
              onSelect={props.onMenuAction}
              style={styles.menu}
              testID="chat-menu"
            >
              <View style={styles.menuIcon}>
                <Ellipsis color={theme.text} size={22} />
              </View>
            </NativeMenu>
          </View>
        }
      >
        {props.children}
      </NativeScreen>
    </DrawerLayoutAndroid>
  );
}

const styles = StyleSheet.create({
  chrome: { borderRadius: 24, overflow: "hidden" },
  group: { flexDirection: "row" },
  menu: { width: 48, height: 48 },
  menuIcon: {
    width: 48,
    height: 48,
    alignItems: "center",
    justifyContent: "center",
  },
});
