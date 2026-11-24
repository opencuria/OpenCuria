import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ChevronLeft, SquarePen } from "lucide-react-native";
import type { HarnessConversation } from "@opencuria/client";
import { ConversationRow } from "../../components/WorkspaceSidebar";
import { ComposerAction } from "../../ui/ComposerAction";
import { useTheme } from "../../ui/theme";

/** The sheet's header while the chat list shows: back, title, new chat. */
export function CompanionChatListHeader({
  workspaceName,
  canCreate,
  onBack,
  onCreate,
}: {
  workspaceName: string;
  canCreate: boolean;
  onBack: () => void;
  onCreate: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.header}>
      <ComposerAction
        label="Back to chat"
        icon={ChevronLeft}
        onPress={onBack}
        testID="companion-chats-back"
      />
      <View style={styles.titles} accessibilityRole="header">
        <Text style={[styles.title, { color: theme.text }]}>Chats</Text>
        <Text
          numberOfLines={1}
          style={[styles.subtitle, { color: theme.mutedText }]}
        >
          {workspaceName}
        </Text>
      </View>
      <ComposerAction
        label="New chat"
        icon={SquarePen}
        disabled={!canCreate}
        onPress={onCreate}
        testID="companion-chats-new"
      />
    </View>
  );
}

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
 * status, unread and attention, time, and the long-press menu.
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
  return (
    <View style={styles.root} testID="companion-chat-list">
      <ScrollView
        style={styles.root}
        contentContainerStyle={{ paddingBottom: insets.bottom + 16 }}
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
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 8,
    paddingBottom: 6,
    gap: 4,
  },
  titles: { flex: 1, alignItems: "center" },
  title: { fontSize: 17, fontWeight: "600" },
  subtitle: { fontSize: 13, marginTop: 1 },
  empty: { textAlign: "center", marginTop: 40, fontSize: 15 },
});
