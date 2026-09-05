/**
 * Harness conversations Pinia store.
 *
 * Powers the dashboard kanban/list feed of root harness sessions.
 * Unread state is sourced from backend `last_read_at` tracking.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { HarnessConversation, HarnessSessionStatus } from '@/types/harness'
import { listHarnessConversations, markHarnessSessionRead } from '@/services/harness.api'
import { WorkspaceStatus } from '@/types'
import { useWorkspaceStore } from '@/stores/workspaces'

export const useHarnessConversationStore = defineStore('harnessConversations', () => {
  const conversations = ref<HarnessConversation[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const searchQuery = ref('')

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
    if (conv) conv.unread = false

    try {
      await markHarnessSessionRead(sessionId)
    } catch {
      if (conv) conv.unread = true
    }
  }

  function updateSessionStatus(sessionId: string, status: HarnessSessionStatus): void {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    if (!conv) return
    conv.status = status
    conv.updated_at = new Date().toISOString()
    if (status === 'idle') {
      conv.unread = true
    } else {
      conv.unread = false
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

  return {
    conversations,
    loading,
    error,
    searchQuery,
    filteredConversations,
    uniqueWorkspaceIds,
    fetchConversations,
    markAsRead,
    updateSessionStatus,
    touchConversation,
  }
})
