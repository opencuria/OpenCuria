// Shared conversation groups (chat-core) specialized to the web workspace type.
import type { Workspace } from '@/types'
import type { WorkspaceConversationGroup as SharedGroup } from '@opencuria/chat-core/conversationGroups'
export type WorkspaceConversationGroup = SharedGroup<Workspace>
