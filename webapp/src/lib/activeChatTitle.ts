import type { HarnessConversation, HarnessSession } from '@/types/harness'

/**
 * The conversations feed can know a generated title before the workspace's
 * session snapshot does. Prefer that title only when its backend timestamp is
 * newer, so an older feed response cannot undo a recent local rename.
 */
export function resolveActiveChatTitle(
  session: HarnessSession | null,
  workspaceId: string,
  conversations: readonly HarnessConversation[],
): string | null {
  if (!session || session.workspace_id !== workspaceId) return null

  const sessionTitle = session.title?.trim() || null
  const conversation = conversations.find(
    (row) => row.session_id === session.id && row.workspace_id === workspaceId,
  )
  const conversationTitle = conversation?.title?.trim() || null
  if (!conversationTitle) return sessionTitle
  if (!sessionTitle) return conversationTitle

  const conversationUpdatedAt = conversation?.updated_at
    ? Date.parse(conversation.updated_at)
    : Number.NaN
  const sessionUpdatedAt = session.updated_at ? Date.parse(session.updated_at) : Number.NaN
  if (
    Number.isFinite(conversationUpdatedAt) &&
    Number.isFinite(sessionUpdatedAt) &&
    conversationUpdatedAt > sessionUpdatedAt
  ) {
    return conversationTitle
  }

  return sessionTitle
}
