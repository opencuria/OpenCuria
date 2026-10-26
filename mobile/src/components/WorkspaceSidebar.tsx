import React, { useEffect, useMemo, useState } from "react";
import { View } from "react-native";
import type { HarnessConversation, Workspace } from "@opencuria/client";
import {
  countableWorkspaces,
  extractInboxConversations,
  formatTimeAgo,
  groupConversationsByWorkspace,
  WORKSPACE_CONVERSATION_PAGE_SIZE,
} from "@opencuria/chat-core/conversationGroups";
import {
  NativeList,
  type NativeListRow,
  type NativeListSection,
} from "../ui/NativeList";
import { NativeTextField } from "../ui/NativeTextField";

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
}: {
  workspaces: Workspace[];
  conversations: HarnessConversation[];
  activeWorkspaceId: string | null;
  activeSessionId: string | null;
  scopeKey: string;
  canCreate: boolean;
  onCreate: () => void;
  onWorkspace: (workspace: Workspace) => void;
  onConversation: (conversation: HarnessConversation) => void;
  onConversationAction: (
    conversation: HarnessConversation,
    action: string,
  ) => void;
  onAllWorkspaces: () => void;
  onMarkAllRead?: () => void;
}): React.JSX.Element {
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
  const conversationRow = (
    row: HarnessConversation,
    inbox = false,
  ): NativeListRow => ({
    id: `${inbox ? "inbox-" : ""}${row.session_id}`,
    title: row.title.trim() || "New chat",
    subtitle: [
      inbox ? row.workspace_name : "",
      row.needs_attention
        ? "Action required"
        : row.status === "busy"
          ? "Working"
          : row.unread
            ? "Unread"
            : formatTimeAgo(row.last_message_at),
    ]
      .filter(Boolean)
      .join(" · "),
    busy: row.status === "busy",
    unread: row.unread || row.needs_attention,
    selected: row.session_id === activeSessionId,
    onPress: () => onConversation(row),
    actions: [
      { id: "rename", title: "Rename", symbol: "pencil" },
      {
        id: "read",
        title: row.unread ? "Mark read" : "Mark unread",
        symbol: "envelope",
      },
      { id: "delete", title: "Delete", symbol: "trash", destructive: true },
    ].map((action) => ({
      ...action,
      disabled: mutationsDisabled(row.workspace_id),
    })),
    onAction: (action) => onConversationAction(row, action),
  });
  const inbox = extractInboxConversations(
    conversations,
    new Set(
      workspaces
        .filter((item) => item.status === "running")
        .map((item) => item.id),
    ),
  ).filter(matches);
  const sections: NativeListSection[] = [
    {
      id: "create",
      rows: [
        {
          id: "drawer-new-chat",
          title: "New chat",
          symbol: "square.and.pencil",
          disabled: !canCreate,
          onPress: onCreate,
        },
      ],
    },
    ...(inbox.length
      ? [
          {
            id: "inbox",
            title: "Inbox",
            rows: inbox.map((row) => conversationRow(row, true)),
          },
        ]
      : []),
    {
      id: "workspace-heading",
      title: "Workspaces",
      rows:
        onMarkAllRead &&
        conversations.some(
          (row) =>
            row.unread &&
            workspaces.some(
              (item) =>
                item.id === row.workspace_id && item.status === "running",
            ),
        )
          ? [
              {
                id: "mark-all-read",
                title: "Mark all as read",
                symbol: "checkmark.circle",
                onPress: onMarkAllRead,
              },
            ]
          : [],
    },
    ...(!groups.length
      ? [
          {
            id: "empty-workspaces",
            rows: [
              { id: "no-running-workspaces", title: "No running workspaces" },
            ],
          },
        ]
      : []),
    ...groups.flatMap((group): NativeListSection[] => {
      const rows = group.conversations.filter(matches);
      if (query && !rows.length && !group.name.toLowerCase().includes(query))
        return [];
      const count =
        visibleCounts[group.workspaceId] ?? WORKSPACE_CONVERSATION_PAGE_SIZE;
      const next = Math.min(
        WORKSPACE_CONVERSATION_PAGE_SIZE,
        rows.length - count,
      );
      return [
        {
          id: group.workspaceId,
          title: group.name,
          expanded: !collapsed[group.workspaceId],
          onExpandedChange: (expanded) =>
            setCollapsed((current) => ({
              ...current,
              [group.workspaceId]: !expanded,
            })),
          onPress: () => {
            if (group.workspace) onWorkspace(group.workspace);
          },
          selected: activeWorkspaceId === group.workspaceId,
          busy: Boolean(
            group.workspace?.has_active_session ||
            group.workspace?.active_operation,
          ),
          rows: [
            ...rows.slice(0, count).map((row) => conversationRow(row)),
            ...(!rows.length
              ? [{ id: `empty-${group.workspaceId}`, title: "No chats yet" }]
              : []),
            ...(next > 0
              ? [
                  {
                    id: `more-${group.workspaceId}`,
                    title: `Show ${next} more ${next === 1 ? "chat" : "chats"}`,
                    onPress: () =>
                      setVisibleCounts((current) => ({
                        ...current,
                        [group.workspaceId]:
                          count + WORKSPACE_CONVERSATION_PAGE_SIZE,
                      })),
                  },
                ]
              : []),
          ],
        },
      ];
    }),
    {
      id: "all-workspaces",
      rows: [
        {
          id: "all-workspaces",
          title: `All workspaces (${countableWorkspaces(workspaces).length})`,
          symbol: "square.grid.2x2",
          onPress: onAllWorkspaces,
        },
      ],
    },
  ];
  return (
    <View style={{ flex: 1 }} testID="workspace-conversation-list">
      <View style={{ margin: 16 }}>
        <NativeTextField
          label="Search"
          value={search}
          onChangeText={setSearch}
          placeholder="Workspaces and chats"
          accessibilityLabel="Search workspaces and conversations"
        />
      </View>
      <NativeList sidebar sections={sections} />
    </View>
  );
}
