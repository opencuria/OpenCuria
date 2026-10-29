import React, { useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { KeyboardController } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Layers,
  MessageCircle,
  MessageCircleQuestion,
  Plus,
  Search,
  ShieldAlert,
  SquarePen,
} from "lucide-react-native";
import type { HarnessConversation, Workspace } from "@opencuria/client";
import {
  conversationTitle,
  countableWorkspaces,
  extractInboxConversations,
  formatTimeAgo,
  groupConversationsByWorkspace,
  isOperatingWorkspace,
  WORKSPACE_CONVERSATION_PAGE_SIZE,
  type WorkspaceConversationGroup,
} from "@opencuria/chat-core/conversationGroups";
import { isWorkspaceReadyForChat } from "@opencuria/chat-core/workspaceReadiness";
import { NativeContextMenu } from "../ui/ContextMenu";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { OpenCuriaMark } from "../ui/OpenCuriaMark";
import { Banner } from "../ui/primitives";
import { useTheme, type OpenCuriaTheme } from "../ui/theme";

/** Chat titles use the platform body size, as in the Claude app. */
const TITLE_SIZE = Platform.OS === "ios" ? 17 : 16;
/** Space reserved beneath the list for the floating account and New chat controls. */
const FOOTER_HEIGHT = 72;
/** Matches the moment UIKit commits a context menu. */
const LONG_PRESS_MS = Platform.OS === "ios" ? 500 : 400;
const noop = () => undefined;

export interface WorkspaceSidebarProps {
  workspaces: Workspace[];
  conversations: HarnessConversation[];
  activeWorkspaceId: string | null;
  activeSessionId: string | null;
  /** Server and organization; changing it resets search, paging and collapse. */
  scopeKey: string;
  canCreate: boolean;
  onCreate: () => void;
  /** Starts a chat in the workspace (or opens it while it is unavailable). */
  onWorkspace: (workspace: Workspace) => void;
  onConversation: (conversation: HarnessConversation) => void;
  onConversationAction: (
    conversation: HarnessConversation,
    action: string,
  ) => void;
  onAllWorkspaces: () => void;
  onMarkAllRead?: () => void;
  profileTitle: string;
  onProfile: () => void;
  error?: string | null;
  onDismissError?: () => void;
}

/**
 * Sidebar in the style of the Claude app: brand and search on top, compact
 * icon rows, chats grouped by workspace as in the webapp, and floating account
 * and New chat controls. Long-press a chat for its native context menu.
 */
export function WorkspaceSidebar({
  workspaces,
  conversations,
  activeWorkspaceId,
  activeSessionId,
  scopeKey,
  canCreate,
  onCreate,
  onWorkspace,
  onConversation,
  onConversationAction,
  onAllWorkspaces,
  onMarkAllRead,
  profileTitle,
  onProfile,
  error,
  onDismissError,
}: WorkspaceSidebarProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const [searching, setSearching] = useState(false);
  const [search, setSearch] = useState("");
  const [visibleCounts, setVisibleCounts] = useState<Record<string, number>>(
    {},
  );
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const groups = useMemo(
    () => groupConversationsByWorkspace(workspaces, conversations),
    [workspaces, conversations],
  );
  useEffect(() => {
    setSearching(false);
    setSearch("");
    setVisibleCounts({});
    setCollapsed({});
  }, [scopeKey]);
  useEffect(() => {
    for (const group of groups) {
      const index = group.conversations.findIndex(
        (row) => row.session_id === activeSessionId,
      );
      if (index < 0) continue;
      const count =
        Math.ceil((index + 1) / WORKSPACE_CONVERSATION_PAGE_SIZE) *
        WORKSPACE_CONVERSATION_PAGE_SIZE;
      setVisibleCounts((current) =>
        (current[group.workspaceId] ?? WORKSPACE_CONVERSATION_PAGE_SIZE) >=
        count
          ? current
          : { ...current, [group.workspaceId]: count },
      );
    }
  }, [groups, activeSessionId]);

  const query = search.trim().toLowerCase();
  const matches = (row: HarnessConversation) =>
    `${row.title} ${row.workspace_name}`.toLowerCase().includes(query);
  const mutationsDisabled = (id: string) => {
    const item = workspaces.find((workspace) => workspace.id === id);
    return Boolean(item?.active_operation || item?.intervention_required);
  };
  const running = new Set(
    workspaces
      .filter((item) => item.status === "running")
      .map((item) => item.id),
  );
  const inbox = extractInboxConversations(conversations, running).filter(
    matches,
  );
  const canMarkAllRead = Boolean(
    onMarkAllRead &&
    conversations.some((row) => row.unread && running.has(row.workspace_id)),
  );
  const visibleGroups = groups.flatMap((group) => {
    const rows = group.conversations.filter(matches);
    return query && !rows.length && !group.name.toLowerCase().includes(query)
      ? []
      : [{ group, rows }];
  });
  const closeSearch = () => {
    // Close the keyboard first; Android would hand focus to the chat composer.
    void KeyboardController.dismiss().catch(() => undefined);
    setSearching(false);
    setSearch("");
  };
  const conversationRow = (row: HarnessConversation, inboxRow = false) => (
    <ConversationRow
      key={`${inboxRow ? "inbox-" : ""}${row.session_id}`}
      conversation={row}
      inbox={inboxRow}
      selected={row.session_id === activeSessionId}
      actionsDisabled={mutationsDisabled(row.workspace_id)}
      onPress={() => {
        closeSearch();
        onConversation(row);
      }}
      onAction={(action) => onConversationAction(row, action)}
    />
  );

  return (
    <View
      style={[styles.root, { backgroundColor: theme.sidebar }]}
      testID="workspace-conversation-list"
    >
      <View style={[styles.header, { paddingTop: insets.top + 8 }]}>
        {searching ? (
          <>
            <View
              style={[
                styles.searchField,
                { backgroundColor: theme.controlFill },
              ]}
            >
              <Search size={17} color={theme.mutedText} />
              <TextInput
                value={search}
                onChangeText={setSearch}
                autoFocus
                placeholder="Search chats"
                placeholderTextColor={theme.subtleText}
                returnKeyType="search"
                autoCorrect={false}
                clearButtonMode="while-editing"
                accessibilityLabel="Search workspaces and conversations"
                style={[styles.searchInput, { color: theme.text }]}
              />
            </View>
            <Pressable
              accessibilityRole="button"
              onPress={closeSearch}
              hitSlop={8}
              style={styles.cancel}
            >
              <Text style={[styles.cancelText, { color: theme.text }]}>
                Cancel
              </Text>
            </Pressable>
          </>
        ) : (
          <>
            <View style={styles.brand} accessibilityRole="header">
              <OpenCuriaMark size={30} />
              <Text style={[styles.brandText, { color: theme.text }]}>
                OpenCuria
              </Text>
            </View>
            <CircleButton
              label="Search chats"
              onPress={() => setSearching(true)}
              theme={theme}
            >
              <Search size={21} color={theme.text} />
            </CircleButton>
          </>
        )}
      </View>

      <ScrollView
        style={styles.root}
        contentContainerStyle={{
          paddingBottom: FOOTER_HEIGHT + insets.bottom + 16,
        }}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="on-drag"
        showsVerticalScrollIndicator={false}
      >
        {error ? (
          <View style={styles.notice}>
            <Banner tone="error" message={error} onDismiss={onDismissError} />
          </View>
        ) : null}
        <SidebarRow
          title="Workspaces"
          icon={<Layers size={20} color={theme.text} />}
          meta={String(countableWorkspaces(workspaces).length)}
          accessibilityLabel={`All workspaces, ${countableWorkspaces(workspaces).length}`}
          onPress={onAllWorkspaces}
          testID="all-workspaces"
        />

        {inbox.length ? (
          <View testID="section-inbox">
            <SectionHeader
              title="Inbox"
              count={inbox.length}
              trailing={
                canMarkAllRead ? (
                  <HeaderAction
                    label="Mark all as read"
                    onPress={onMarkAllRead!}
                    theme={theme}
                  >
                    <CheckCheck size={18} color={theme.mutedText} />
                  </HeaderAction>
                ) : null
              }
            />
            {inbox.map((row) => conversationRow(row, true))}
          </View>
        ) : null}

        {!groups.length ? (
          <Text style={[styles.placeholder, { color: theme.subtleText }]}>
            No running workspaces
          </Text>
        ) : null}
        {query && !inbox.length && !visibleGroups.length ? (
          <Text style={[styles.placeholder, { color: theme.subtleText }]}>
            No matching chats
          </Text>
        ) : null}
        {visibleGroups.map(({ group, rows }) => {
          const expanded = !collapsed[group.workspaceId];
          const count =
            visibleCounts[group.workspaceId] ??
            WORKSPACE_CONVERSATION_PAGE_SIZE;
          const next = Math.min(
            WORKSPACE_CONVERSATION_PAGE_SIZE,
            rows.length - count,
          );
          return (
            <View
              key={group.workspaceId}
              testID={`section-${group.workspaceId}`}
            >
              <WorkspaceHeader
                group={group}
                expanded={expanded}
                active={activeWorkspaceId === group.workspaceId}
                onToggle={() =>
                  setCollapsed((current) => ({
                    ...current,
                    [group.workspaceId]: expanded,
                  }))
                }
                onCreate={() => {
                  if (group.workspace) onWorkspace(group.workspace);
                }}
              />
              {expanded ? (
                <>
                  {rows.slice(0, count).map((row) => conversationRow(row))}
                  {!rows.length ? (
                    <Text style={[styles.empty, { color: theme.subtleText }]}>
                      No chats yet
                    </Text>
                  ) : null}
                  {next > 0 ? (
                    <Pressable
                      accessibilityRole="button"
                      accessibilityLabel={`Show ${next} more ${next === 1 ? "chat" : "chats"} in ${group.name}`}
                      onPress={() =>
                        setVisibleCounts((current) => ({
                          ...current,
                          [group.workspaceId]:
                            count + WORKSPACE_CONVERSATION_PAGE_SIZE,
                        }))
                      }
                      style={({ pressed }) => [
                        styles.more,
                        pressed && { opacity: 0.5 },
                      ]}
                    >
                      <Text
                        style={[styles.moreText, { color: theme.mutedText }]}
                      >
                        Show {next} more
                      </Text>
                    </Pressable>
                  ) : null}
                </>
              ) : null}
            </View>
          );
        })}
      </ScrollView>

      <View
        pointerEvents="box-none"
        style={[
          styles.footer,
          {
            paddingBottom: Math.max(insets.bottom, 12),
            experimental_backgroundImage: `linear-gradient(to bottom, ${theme.sidebar}00 0%, ${theme.sidebar} 45%)`,
          },
        ]}
      >
        <CircleButton
          label={`Account and settings, ${profileTitle}`}
          onPress={onProfile}
          theme={theme}
          size={48}
          testID="drawer-profile"
        >
          <Text style={[styles.avatarText, { color: theme.text }]}>
            {profileTitle.slice(0, 1).toUpperCase()}
          </Text>
        </CircleButton>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="New chat"
          accessibilityState={{ disabled: !canCreate }}
          disabled={!canCreate}
          onPress={onCreate}
          testID="drawer-new-chat"
          style={({ pressed }) => [
            styles.newChat,
            { backgroundColor: theme.text },
            !canCreate && styles.disabled,
            pressed && styles.pressed,
          ]}
        >
          <Plus size={20} color={theme.background} />
          <Text style={[styles.newChatText, { color: theme.background }]}>
            New chat
          </Text>
        </Pressable>
      </View>
    </View>
  );
}

/** One chat: status icon, single-line title and a quiet time or workspace. */
function ConversationRow({
  conversation,
  inbox,
  selected,
  actionsDisabled,
  onPress,
  onAction,
}: {
  conversation: HarnessConversation;
  inbox: boolean;
  selected: boolean;
  actionsDisabled: boolean;
  onPress: () => void;
  onAction: (action: string) => void;
}): React.JSX.Element {
  const title = conversationTitle(conversation);
  const busy = conversation.status === "busy";
  const status = conversation.needs_attention
    ? attentionLabel(conversation)
    : busy
      ? "Working"
      : conversation.unread
        ? "Unread"
        : "";
  const meta = inbox
    ? conversation.workspace_name
    : formatTimeAgo(conversation.last_message_at);
  const actions: NativeMenuAction[] = [
    { id: "rename", title: "Rename", symbol: "pencil" },
    {
      id: "read",
      title: conversation.unread ? "Mark as read" : "Mark as unread",
      symbol: conversation.unread ? "envelope.open" : "envelope.badge",
    },
    { id: "delete", title: "Delete", symbol: "trash", destructive: true },
  ].map((action) => ({
    ...action,
    disabled: action.id !== "read" && actionsDisabled,
  }));
  return (
    <NativeContextMenu label={title} actions={actions} onSelect={onAction}>
      {(showMenu) => (
        <SidebarRow
          title={title}
          strong={conversation.unread || conversation.needs_attention}
          icon={
            <ConversationIcon conversation={conversation} selected={selected} />
          }
          meta={meta}
          selected={selected}
          onPress={onPress}
          onLongPress={showMenu}
          accessibilityLabel={[title, status, meta].filter(Boolean).join(", ")}
          accessibilityActions={actions.filter((action) => !action.disabled)}
          onAccessibilityAction={onAction}
          testID={`${inbox ? "inbox-" : ""}${conversation.session_id}`}
        />
      )}
    </NativeContextMenu>
  );
}

function ConversationIcon({
  conversation,
  selected,
}: {
  conversation: HarnessConversation;
  selected: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  if (conversation.needs_attention) {
    const Icon =
      conversation.attention_kind === "question"
        ? MessageCircleQuestion
        : conversation.attention_kind === "permission"
          ? ShieldAlert
          : CircleAlert;
    return <Icon size={20} color={theme.warning} />;
  }
  if (conversation.status === "busy")
    return <ActivityIndicator size="small" color={theme.mutedText} />;
  return (
    <MessageCircle
      size={20}
      color={
        conversation.unread
          ? theme.primary
          : selected
            ? theme.text
            : theme.mutedText
      }
    />
  );
}

/** Compact, borderless row with a rounded highlight for press and selection. */
function SidebarRow({
  title,
  icon,
  meta,
  strong,
  selected,
  onPress,
  onLongPress,
  accessibilityLabel,
  accessibilityActions,
  onAccessibilityAction,
  testID,
}: {
  title: string;
  icon: React.ReactNode;
  meta?: string;
  strong?: boolean;
  selected?: boolean;
  onPress: () => void;
  onLongPress?: () => void;
  accessibilityLabel?: string;
  accessibilityActions?: NativeMenuAction[];
  onAccessibilityAction?: (id: string) => void;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? title}
      accessibilityState={{ selected: Boolean(selected) }}
      accessibilityActions={accessibilityActions?.map((action) => ({
        name: action.id,
        label: action.title,
      }))}
      onAccessibilityAction={(event) =>
        onAccessibilityAction?.(event.nativeEvent.actionName)
      }
      onPress={onPress}
      // iOS presents the context menu itself; a long press must not also open the chat.
      onLongPress={onLongPress ?? noop}
      delayLongPress={LONG_PRESS_MS}
      // A foreground ripple keeps the selection fill beneath it.
      android_ripple={{ color: theme.controlFill, foreground: true }}
      testID={testID}
      style={({ pressed }) => [
        styles.row,
        (selected || (pressed && Platform.OS === "ios")) && {
          backgroundColor: theme.controlFill,
        },
      ]}
    >
      <View style={styles.rowIcon}>{icon}</View>
      <Text
        numberOfLines={1}
        ellipsizeMode="tail"
        style={[
          styles.rowTitle,
          { color: theme.text, fontWeight: strong ? "600" : "400" },
        ]}
      >
        {title}
      </Text>
      {meta ? (
        <Text
          numberOfLines={1}
          style={[styles.rowMeta, { color: theme.subtleText }]}
        >
          {meta}
        </Text>
      ) : null}
    </Pressable>
  );
}

/** Smaller workspace label that collapses its chats; the pen starts a chat there. */
function WorkspaceHeader({
  group,
  expanded,
  active,
  onToggle,
  onCreate,
}: {
  group: WorkspaceConversationGroup<Workspace>;
  expanded: boolean;
  active: boolean;
  onToggle: () => void;
  onCreate: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const workspace = group.workspace;
  const operating = Boolean(workspace && isOperatingWorkspace(workspace));
  const busy = Boolean(
    workspace?.has_active_session ||
    operating ||
    group.conversations.some((row) => row.status === "busy"),
  );
  const status = workspace?.active_operation
    ? workspace.active_operation.replaceAll("_", " ")
    : group.online
      ? ""
      : "Offline";
  const startable = Boolean(
    workspace && isWorkspaceReadyForChat(workspace, operating),
  );
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <View style={styles.groupHeader}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`${expanded ? "Collapse" : "Expand"} workspace ${group.name}`}
        accessibilityState={{ expanded }}
        onPress={onToggle}
        hitSlop={{ top: 6, bottom: 6 }}
        style={({ pressed }) => [
          styles.groupToggle,
          pressed && { opacity: 0.5 },
        ]}
      >
        <Text
          numberOfLines={1}
          style={[
            styles.groupTitle,
            { color: active ? theme.text : theme.mutedText },
          ]}
        >
          {group.name}
        </Text>
        <Chevron size={15} color={theme.subtleText} strokeWidth={2.4} />
        {status ? (
          <Text style={[styles.groupStatus, { color: theme.subtleText }]}>
            {status}
          </Text>
        ) : null}
        {busy ? (
          <ActivityIndicator
            size="small"
            color={theme.subtleText}
            style={styles.groupBusy}
            testID={`workspace-busy-${group.workspaceId}`}
          />
        ) : null}
      </Pressable>
      <HeaderAction
        label={`New chat in ${group.name}`}
        onPress={onCreate}
        disabled={!startable}
        theme={theme}
      >
        <SquarePen size={17} color={theme.mutedText} />
      </HeaderAction>
    </View>
  );
}

function SectionHeader({
  title,
  count,
  trailing,
}: {
  title: string;
  count: number;
  trailing?: React.ReactNode;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.groupHeader}>
      <View style={styles.groupToggle} accessibilityRole="header">
        <Text style={[styles.groupTitle, { color: theme.mutedText }]}>
          {title}
        </Text>
        <View style={[styles.count, { backgroundColor: theme.controlFill }]}>
          <Text style={[styles.countText, { color: theme.mutedText }]}>
            {count}
          </Text>
        </View>
      </View>
      {trailing}
    </View>
  );
}

function HeaderAction({
  label,
  onPress,
  disabled,
  theme,
  children,
}: React.PropsWithChildren<{
  label: string;
  onPress: () => void;
  disabled?: boolean;
  theme: OpenCuriaTheme;
}>): React.JSX.Element {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: Boolean(disabled) }}
      disabled={disabled}
      onPress={onPress}
      hitSlop={6}
      android_ripple={{
        color: theme.controlFill,
        borderless: true,
        radius: 18,
      }}
      style={({ pressed }) => [
        styles.headerAction,
        disabled && styles.disabled,
        pressed && Platform.OS === "ios" && { opacity: 0.5 },
      ]}
    >
      {children}
    </Pressable>
  );
}

function CircleButton({
  label,
  onPress,
  theme,
  size = 44,
  testID,
  children,
}: React.PropsWithChildren<{
  label: string;
  onPress: () => void;
  theme: OpenCuriaTheme;
  size?: number;
  testID?: string;
}>): React.JSX.Element {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={onPress}
      testID={testID}
      android_ripple={{ color: theme.controlFill, foreground: true }}
      style={({ pressed }) => [
        styles.circle,
        {
          width: size,
          height: size,
          borderRadius: size / 2,
          backgroundColor: theme.raised,
          borderColor: theme.border,
        },
        pressed && styles.pressed,
      ]}
    >
      {children}
    </Pressable>
  );
}

function attentionLabel(conversation: HarnessConversation): string {
  if (conversation.attention_kind === "question") return "Question waiting";
  if (conversation.attention_kind === "permission")
    return "Permission required";
  if (conversation.attention_kind === "both")
    return "Permission and question waiting";
  return "Action required";
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 52,
    paddingHorizontal: 18,
    paddingBottom: 14,
  },
  brand: { flex: 1, flexDirection: "row", alignItems: "center", gap: 10 },
  brandText: { fontSize: 26, fontWeight: "700", letterSpacing: -0.4 },
  searchField: {
    flex: 1,
    height: 44,
    borderRadius: 22,
    paddingHorizontal: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  searchInput: { flex: 1, fontSize: 16, paddingVertical: 0 },
  cancel: { height: 44, justifyContent: "center" },
  cancelText: { fontSize: 16 },
  notice: { paddingHorizontal: 12, paddingBottom: 8 },
  row: {
    minHeight: 46,
    marginHorizontal: 8,
    paddingHorizontal: 12,
    borderRadius: 14,
    overflow: "hidden",
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
  },
  rowIcon: {
    width: 24,
    flexShrink: 0,
    alignItems: "center",
    justifyContent: "center",
  },
  rowTitle: {
    flex: 1,
    flexShrink: 1,
    minWidth: 0,
    fontSize: TITLE_SIZE,
    letterSpacing: -0.2,
  },
  rowMeta: { maxWidth: 96, fontSize: 13 },
  groupHeader: {
    flexDirection: "row",
    alignItems: "center",
    marginTop: 18,
    paddingLeft: 20,
    paddingRight: 12,
    minHeight: 36,
  },
  groupToggle: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    minHeight: 36,
  },
  groupTitle: { flexShrink: 1, fontSize: 14, fontWeight: "600" },
  groupStatus: { marginLeft: 4, fontSize: 12 },
  groupBusy: { marginLeft: 4, transform: [{ scale: 0.75 }] },
  count: {
    marginLeft: 4,
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    paddingHorizontal: 6,
    alignItems: "center",
    justifyContent: "center",
  },
  countText: { fontSize: 12, fontWeight: "600" },
  headerAction: {
    width: 36,
    height: 36,
    alignItems: "center",
    justifyContent: "center",
  },
  placeholder: { paddingHorizontal: 20, paddingTop: 22, fontSize: 15 },
  empty: {
    minHeight: 36,
    paddingLeft: 58,
    fontSize: 15,
    lineHeight: 36,
  },
  more: { minHeight: 40, justifyContent: "center", paddingLeft: 58 },
  moreText: { fontSize: 15 },
  footer: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingTop: 28,
    paddingHorizontal: 16,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  circle: {
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
    borderWidth: StyleSheet.hairlineWidth,
  },
  avatarText: { fontSize: 18, fontWeight: "600" },
  newChat: {
    height: 48,
    borderRadius: 24,
    paddingLeft: 18,
    paddingRight: 22,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  newChatText: { fontSize: 16, fontWeight: "600" },
  disabled: { opacity: 0.35 },
  pressed: { opacity: 0.7 },
});
