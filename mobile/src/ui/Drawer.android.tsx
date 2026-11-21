import React, { useEffect, useRef } from "react";
import { BackHandler, StyleSheet, View } from "react-native";
import {
  ArrowLeft,
  Ellipsis,
  GitBranch,
  Menu,
  MessageCirclePlus,
} from "lucide-react-native";
import type { DrawerProps } from "./Drawer.types";
import { IconButton } from "./controls";
import { NativeMenu } from "./NativeMenu";
import { NativeScreen } from "./NativeScreen";
import { NavigationTitle } from "./NavigationTitle";
import { SlideDrawer } from "./SlideDrawer";
import { useTheme } from "./theme";

/** The sidebar slides in beneath the chat; Back closes it first. */
export function Drawer(props: DrawerProps): React.JSX.Element {
  const { theme } = useTheme();
  const chrome = [styles.chrome, { backgroundColor: theme.surfaceMuted }];
  const { detail } = props;
  const latestDetail = useRef(detail);
  latestDetail.current = detail;
  const inDetail = Boolean(detail);

  // System Back leaves a subagent for its parent; an open sidebar closes first.
  useEffect(() => {
    if (!inDetail || props.visible) return;
    const handler = BackHandler.addEventListener("hardwareBackPress", () => {
      latestDetail.current?.onBack();
      return true;
    });
    return () => handler.remove();
  }, [inDetail, props.visible]);

  // An empty menu (offline) is left out.
  const menu = props.menuActions.length ? (
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
  ) : null;

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
          detail ? (
            // Back and title share one floating bar, so the title stays legible
            // above the transcript scrolling beneath the transparent header.
            <View style={[chrome, styles.detailBar]}>
              <IconButton
                label={`Back to ${detail.backTitle}`}
                icon={<ArrowLeft color={theme.text} size={22} />}
                onPress={detail.onBack}
                testID="chat-back"
              />
              <NavigationTitle
                title={detail.title}
                subtitle={detail.subtitle}
                testID="chat-detail-title"
              />
            </View>
          ) : (
            <View style={chrome}>
              <IconButton
                label="Open sidebar"
                icon={<Menu color={theme.text} size={22} />}
                onPress={props.onOpen}
                testID="main-menu"
              />
            </View>
          )
        }
        right={
          // A subagent page keeps only its overflow menu, like a pushed detail view.
          detail ? (
            menu && <View style={chrome}>{menu}</View>
          ) : (
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
              {menu}
            </View>
          )
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
  detailBar: {
    flexDirection: "row",
    alignItems: "center",
    flexShrink: 1,
    paddingEnd: 20,
    marginEnd: 12,
  },
  menu: { width: 48, height: 48 },
  menuIcon: {
    width: 48,
    height: 48,
    alignItems: "center",
    justifyContent: "center",
  },
});
