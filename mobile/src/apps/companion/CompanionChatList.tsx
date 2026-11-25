import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import Animated, { useAnimatedStyle } from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { HarnessConversation } from "@opencuria/client";
import { ConversationRow } from "../../components/WorkspaceSidebar";
import { useChromeTopInset, useHiddenBottom } from "../../ui/chromeInset";
import { useTheme } from "../../ui/theme";

export interface CompanionChatListProps {
  /** Chats of the app's workspace, newest first. */
  conversations: HarnessConversation[];
  activeSessionId: string | null;
  offline: boolean;
  /** Renaming, marking and deleting need the server and a quiet workspace. */
  actionsDisabled: boolean;
  onSelect: (conversation: HarnessConversation) => void;
  onAction: (conversation: HarnessConversation, action: string) => void;
}

/**
 * Every chat of the app's workspace, with the rows of OpenCuria's sidebar:
 * status, unread and attention, time, and the long-press menu. It scrolls
 * beneath the sheet's translucent bar and ends above the screen's edge.
 */
export function CompanionChatList({
  conversations,
  activeSessionId,
  offline,
  actionsDisabled,
  onSelect,
  onAction,
}: CompanionChatListProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const top = useChromeTopInset();
  const hiddenBottom = useHiddenBottom();
  // The rows below the screen's edge (a half-height sheet) stay reachable.
  const hiddenSpace = useAnimatedStyle(() => ({
    height: Math.max(0, hiddenBottom.value),
  }));
  return (
    <View style={styles.root} testID="companion-chat-list">
      <ScrollView
        style={styles.root}
        contentContainerStyle={{
          paddingTop: top,
          paddingBottom: insets.bottom + 16,
        }}
        showsVerticalScrollIndicator={false}
      >
        {conversations.length ? (
          conversations.map((row) => (
            <ConversationRow
              key={row.session_id}
              conversation={row}
              inbox={false}
              selected={row.session_id === activeSessionId}
              actionsDisabled={actionsDisabled}
              offline={offline}
              onPress={() => onSelect(row)}
              onAction={(action) => onAction(row, action)}
            />
          ))
        ) : (
          <Text style={[styles.empty, { color: theme.subtleText }]}>
            {offline ? "No chats are saved on this device." : "No chats yet."}
          </Text>
        )}
        <Animated.View style={hiddenSpace} />
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  empty: { textAlign: "center", marginTop: 40, fontSize: 15 },
});
