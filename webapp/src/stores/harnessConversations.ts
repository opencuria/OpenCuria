/**
 * Harness conversations Pinia store.
 *
 * Powers the dashboard kanban/list feed of root harness sessions.
 * Unread state is sourced from backend `last_read_at` / `manual_unread_at`.
 * Pending permission/question gates set `needs_attention`.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type {
  HarnessAttentionKind,
  HarnessConversation,
  HarnessSessionStatus,
} from '@/types/harness'
import { extractActiveConversations } from '@/lib/conversationGroups'
import {
  listHarnessConversations,
  markHarnessSessionRead,
  markHarnessSessionUnread,
} from '@/services/harness.api'
import { WorkspaceStatus } from '@/types'
import { useWorkspaceStore } from '@/stores/workspaces'

const ATTENTION_REFRESH_MS = 300

export const useHarnessConversationStore = defineStore('harnessConversations', () => {
  const conversations = ref<HarnessConversation[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const searchQuery = ref('')
  let attentionRefreshTimer: ReturnType<typeof setTimeout> | null = null

  const filteredConversations = computed(() => {
    const workspaceStore = useWorkspaceStore()
    const runningWorkspaceIds = new Set(
      workspaceStore.workspaces
        .filter((workspace) => workspace.status === WorkspaceStatus.RUNNING)
        .map((workspace) => workspace.id),
    )
    const visible = conversations.value.filter((conv) =>
      runningWorkspaceIds.has(conv.workspace_id),
    )
    const q = searchQuery.value.trim().toLowerCase()
    if (!q) return visible
    return visible.filter((conv) => {
      return (
        conv.workspace_name.toLowerCase().includes(q) ||
        conv.title.toLowerCase().includes(q) ||
        conv.agent_name.toLowerCase().includes(q) ||
        conv.mode.toLowerCase().includes(q)
      )
    })
  })

  const uniqueWorkspaceIds = computed(() => {
    const ids = new Set<string>()
    for (const conv of conversations.value) {
      ids.add(conv.workspace_id)
    }
    return [...ids]
  })

  const activeConversations = computed(() => extractActiveConversations(conversations.value))

  const attentionCount = computed(
    () => conversations.value.filter((row) => row.needs_attention).length,
  )

  async function fetchConversations(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const raw = await listHarnessConversations()
      conversations.value = raw.sort(
        (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load conversations'
    } finally {
      loading.value = false
    }
  }

  async function markAsRead(sessionId: string): Promise<void> {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    const previousUnread = conv?.unread ?? false
    const previousManual = conv?.manual_unread ?? false
    if (conv) {
      conv.unread = false
      conv.manual_unread = false
    }

    try {
      await markHarnessSessionRead(sessionId)
    } catch {
      if (conv) {
        conv.unread = previousUnread
        conv.manual_unread = previousManual
      }
    }
  }

  async function markAsUnread(sessionId: string): Promise<void> {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    const previousUnread = conv?.unread ?? false
    const previousManual = conv?.manual_unread ?? false
    if (conv) {
      conv.unread = true
      conv.manual_unread = true
    }

    try {
      await markHarnessSessionUnread(sessionId)
    } catch {
      if (conv) {
        conv.unread = previousUnread
        conv.manual_unread = previousManual
      }
    }
  }

  function updateSessionStatus(
    sessionId: string,
    status: HarnessSessionStatus,
    viewed = false,
  ): void {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    if (!conv) return
    conv.status = status
    conv.updated_at = new Date().toISOString()
    if (!conv.manual_unread) {
      if (status === 'idle') {
        conv.unread = !viewed
      } else {
        conv.unread = false
      }
    }
    conversations.value = [...conversations.value].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )
  }

  function touchConversation(sessionId: string): void {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    if (!conv) return
    conv.updated_at = new Date().toISOString()
    conversations.value = [...conversations.value].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )
  }

  function setAttention(sessionId: string, kind: 'permission' | 'question'): void {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    if (!conv) {
      void fetchConversations()
      return
    }
    conv.needs_attention = true
    conv.attention_kind = mergeAttentionKind(conv.attention_kind, kind)
  }

  function clearAttention(sessionId: string): void {
    scheduleAttentionRefresh()
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    if (!conv) return
    conv.needs_attention = false
    conv.attention_kind = ''
  }

  function scheduleAttentionRefresh(): void {
    if (attentionRefreshTimer) clearTimeout(attentionRefreshTimer)
    attentionRefreshTimer = setTimeout(() => {
      attentionRefreshTimer = null
      void fetchConversations()
    }, ATTENTION_REFRESH_MS)
  }

  return {
    conversations,
    loading,
    error,
    searchQuery,
    filteredConversations,
    uniqueWorkspaceIds,
    activeConversations,
    attentionCount,
    fetchConversations,
    markAsRead,
    markAsUnread,
    updateSessionStatus,
    touchConversation,
    setAttention,
    clearAttention,
  }
})

function mergeAttentionKind(
  current: HarnessAttentionKind | undefined,
  next: 'permission' | 'question',
): HarnessAttentionKind {
  if (!current || current === next) return next
  return 'both'
}
