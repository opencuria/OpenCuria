import type { HarnessConversation } from "./harness";

/** Fields needed for navigation, independent of either client's full DTO. */
export interface ConversationWorkspace {
  id: string;
  name: string;
  status: string;
  runner_online: boolean;
  active_operation?: string | null;
  intervention_required?: boolean;
  has_active_session?: boolean;
  last_activity_at?: string | null;
}

export const ACTIVE_CONVERSATION_LIMIT = 5;
export const WORKSPACE_CONVERSATION_PAGE_SIZE = 4;
export const SIDEBAR_WORKSPACE_LIMIT = 4;

export interface WorkspaceConversationGroup<
  W extends ConversationWorkspace = ConversationWorkspace,
> {
  workspaceId: string;
  name: string;
  workspace: W | null;
  online: boolean;
  conversations: HarnessConversation[];
}

const HIDDEN_WORKSPACE_STATUSES = new Set<string>([
  "removed",
  "deleted",
  "deleting",
  "pending_deletion",
]);

/**
 * Display title for a conversation row (fallback when the session has no title yet).
 */
export function conversationTitle(conversation: HarnessConversation): string {
  return conversation.title?.trim() || "New chat";
}

/**
 * Compact relative timestamp for sidebar rows (`now`, `5m`, `2h`, `3d`).
 */
export function formatTimeAgo(isoString: string, now = Date.now()): string {
  const diff = now - new Date(isoString).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  const weeks = Math.floor(days / 7);
  if (weeks < 52) return `${weeks}w`;
  return `${Math.floor(days / 365)}y`;
}

/**
 * Busy sessions first, then unread, newest first. Caps at `limit`.
 * Conversations that need user action are excluded (they live in Action required).
 */
export function extractActiveConversations(
  conversations: readonly HarnessConversation[],
  limit = ACTIVE_CONVERSATION_LIMIT,
): HarnessConversation[] {
  return [...conversations]
    .filter(
      (conversation) =>
        !conversation.needs_attention &&
        (conversation.status === "busy" || conversation.unread),
    )
    .sort((a, b) => {
      if (a.status === "busy" && b.status !== "busy") return -1;
      if (a.status !== "busy" && b.status === "busy") return 1;
      return (
        new Date(b.last_message_at).getTime() -
        new Date(a.last_message_at).getTime()
      );
    })
    .slice(0, limit);
}

/**
 * Conversations waiting on a permission or question gate, newest first.
 */
export function extractActionRequired(
  conversations: HarnessConversation[],
): HarnessConversation[] {
  return [...conversations]
    .filter((conversation) => Boolean(conversation.needs_attention))
    .sort(
      (a, b) =>
        new Date(b.last_message_at).getTime() -
        new Date(a.last_message_at).getTime(),
    );
}

/**
 * Sidebar inbox: actionable gates from running workspaces, then idle unread
 * chats from any workspace. A gate takes precedence over an unread result.
 */
export function extractInboxConversations(
  conversations: readonly HarnessConversation[],
  runningWorkspaceIds: ReadonlySet<string>,
): HarnessConversation[] {
  return conversations
    .filter((conversation) =>
      conversation.needs_attention
        ? runningWorkspaceIds.has(conversation.workspace_id)
        : conversation.unread && conversation.status === "idle",
    )
    .sort(
      (a, b) =>
        Number(Boolean(b.needs_attention)) -
          Number(Boolean(a.needs_attention)) ||
        new Date(b.last_message_at).getTime() -
          new Date(a.last_message_at).getTime() ||
        a.session_id.localeCompare(b.session_id),
    );
}

/**
 * Group running and lifecycle-reserved workspaces' chats, online first.
 * Ordinary stopped, deleted and unknown history stays outside grouped navigation.
 */
export function groupConversationsByWorkspace<W extends ConversationWorkspace>(
  workspaces: readonly W[],
  conversations: readonly HarnessConversation[],
): WorkspaceConversationGroup<W>[] {
  const groups = new Map<string, WorkspaceConversationGroup<W>>();
  for (const workspace of workspaces) {
    if (!isConversationWorkspaceEligible(workspace)) continue;
    groups.set(workspace.id, {
      workspaceId: workspace.id,
      name:
        workspace.name.trim() ||
        conversations
          .find((row) => row.workspace_id === workspace.id)
          ?.workspace_name.trim() ||
        `Workspace ${workspace.id.slice(0, 8)}`,
      workspace,
      online: isLiveWorkspace(workspace),
      conversations: [],
    });
  }

  const newestFirst = [...conversations].sort(
    (a, b) =>
      new Date(b.last_message_at).getTime() -
        new Date(a.last_message_at).getTime() ||
      a.session_id.localeCompare(b.session_id),
  );
  for (const conversation of newestFirst) {
    groups.get(conversation.workspace_id)?.conversations.push(conversation);
  }

  return [...groups.values()].sort(
    (a, b) =>
      Number(b.online) - Number(a.online) ||
      a.name.localeCompare(b.name, undefined, {
        sensitivity: "base",
        numeric: true,
      }) ||
      a.workspaceId.localeCompare(b.workspaceId),
  );
}

/**
 * Workspaces shown in the compact sidebar section: live/operating ones and
 * any workspace that already has chats. Stopped empty workspaces stay on
 * `/workspaces`. Sorted live-first, then by `last_activity_at`.
 */
export function selectSidebarWorkspaces<W extends ConversationWorkspace>(
  workspaces: readonly W[],
  conversations: readonly HarnessConversation[],
  limit = SIDEBAR_WORKSPACE_LIMIT,
): W[] {
  const workspaceIdsWithChats = new Set(
    conversations.map((conversation) => conversation.workspace_id),
  );
  return [...workspaces]
    .filter((workspace) => {
      if (HIDDEN_WORKSPACE_STATUSES.has(workspace.status)) return false;
      if (workspaceIdsWithChats.has(workspace.id)) return true;
      return isLiveWorkspace(workspace) || isOperatingWorkspace(workspace);
    })
    .sort((a, b) => {
      const liveDelta = Number(isLiveWorkspace(b)) - Number(isLiveWorkspace(a));
      if (liveDelta !== 0) return liveDelta;
      return (
        new Date(b.last_activity_at ?? 0).getTime() -
        new Date(a.last_activity_at ?? 0).getTime()
      );
    })
    .slice(0, limit);
}

/** Capture/recovery reserves history navigation even while the runtime is stopped. */
export function isConversationWorkspaceEligible(
  workspace: ConversationWorkspace,
): boolean {
  if (HIDDEN_WORKSPACE_STATUSES.has(workspace.status)) return false;
  return (
    workspace.status === "running" ||
    workspace.active_operation === "capturing_image" ||
    Boolean(workspace.intervention_required)
  );
}

export function isLiveWorkspace(workspace: ConversationWorkspace): boolean {
  return workspace.status === "running" && workspace.runner_online;
}

export function isOperatingWorkspace(
  workspace: ConversationWorkspace,
): boolean {
  return Boolean(workspace.active_operation) || workspace.status === "creating";
}

export function countableWorkspaces<W extends ConversationWorkspace>(
  workspaces: readonly W[],
): W[] {
  return workspaces.filter(
    (workspace) => !HIDDEN_WORKSPACE_STATUSES.has(workspace.status),
  );
}
