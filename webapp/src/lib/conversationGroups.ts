// Both clients use the same workspace eligibility, order and inbox rules.
export * from '@opencuria/chat-core/conversationGroups'
import type { Workspace } from '@/types'
import type { WorkspaceConversationGroup as SharedGroup } from '@opencuria/chat-core/conversationGroups'
export type WorkspaceConversationGroup = SharedGroup<Workspace>
