import React from "react";
import { StyleSheet, View } from "react-native";
import {
  Ellipsis,
  GitBranch,
  Menu,
  MessageCirclePlus,
} from "lucide-react-native";
import type { DrawerProps } from "./Drawer.types";
import { IconButton } from "./controls";
import { NativeMenu } from "./NativeMenu";
import { NativeScreen } from "./NativeScreen";
import { SlideDrawer } from "./SlideDrawer";
import { useTheme } from "./theme";

/** The sidebar slides in beneath the chat; Back closes it first. */
export function Drawer(props: DrawerProps): React.JSX.Element {
  const { theme } = useTheme();
  const chrome = [styles.chrome, { backgroundColor: theme.surfaceMuted }];
  return (
    <SlideDrawer
      open={props.visible}
      onOpen={props.onOpen}
      onClose={props.onClose}
      sidebar={props.navigation}
    >
      <NativeScreen
        title=""
        transparentHeader
        left={
          <View style={chrome}>
            <IconButton
              label="Open sidebar"
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
            <IconButton
              label="Git, Desktop, Terminal & Files"
              icon={<GitBranch color={theme.text} size={22} />}
              onPress={props.onTools}
              disabled={!props.canOpenTools}
              testID="workspace-tools"
            />
            {/* An empty menu (offline) is left out. */}
            {props.menuActions.length ? (
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
            ) : null}
          </View>
        }
      >
        {props.children}
      </NativeScreen>
    </SlideDrawer>
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
