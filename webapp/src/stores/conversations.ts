/**
 * Conversation Pinia store.
 *
 * Manages the dashboard conversation list — one entry per Chat,
 * plus workspace fallbacks for workspaces without any chats.
 * Read/unread state is derived from dedicated backend read tracking.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import type { Conversation } from '@/types'
import { SessionStatus, WorkspaceStatus } from '@/types'
import {
  listConversations,
  markConversationRead as apiMarkRead,
  markConversationUnread as apiMarkUnread,
} from '@/services/conversations.api'
import { isSessionDone } from '@/lib/sessionState'

export const useConversationStore = defineStore('conversations', () => {
  // --- State ---
  const conversations = ref<Conversation[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const searchQuery = ref('')

  // --- Getters ---

  const filteredConversations = computed(() => {
    const q = searchQuery.value.trim().toLowerCase()
    const running = conversations.value.filter(
      (conv) => conv.workspace_status === WorkspaceStatus.RUNNING,
    )
    if (!q) return running
    return running.filter((conv) => {
      return (
        conv.workspace_name.toLowerCase().includes(q) ||
        conv.chat_name.toLowerCase().includes(q) ||
        (conv.last_session?.prompt ?? '').toLowerCase().includes(q)
      )
    })
  })

  /** Unique workspace IDs — used for WebSocket subscriptions on the dashboard. */
  const uniqueWorkspaceIds = computed(() => {
    const ids = new Set<string>()
    for (const conv of conversations.value) {
      ids.add(conv.workspace_id)
    }
    return [...ids]
  })

  // --- Actions ---

  async function fetchConversations(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const raw = await listConversations()
      conversations.value = raw.sort(
        (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load conversations'
    } finally {
      loading.value = false
    }
  }

  /**
   * Mark a conversation as read in the backend and optimistically update
   * the local state so the UI responds immediately without waiting for the
   * next polling cycle.
   */
  async function markAsRead(
    workspaceId: string,
    chatId: string | null,
    sessionId: string,
  ): Promise<void> {
    // Optimistic update
    const conv = conversations.value.find(
      (c) => c.workspace_id === workspaceId && c.chat_id === chatId,
    )
    if (conv) conv.is_read = true

    // Persist to backend (fire-and-forget; revert on error)
    try {
      await apiMarkRead(sessionId)
    } catch {
      if (conv) conv.is_read = false
    }
  }

  async function markAsUnread(
    workspaceId: string,
    chatId: string | null,
    sessionId: string,
  ): Promise<void> {
    const conv = conversations.value.find(
      (c) => c.workspace_id === workspaceId && c.chat_id === chatId,
    )
    if (conv?.last_session?.id === sessionId) conv.is_read = false

    try {
      await apiMarkUnread(sessionId)
    } catch {
      if (conv?.last_session?.id === sessionId) conv.is_read = true
    }
  }

  /**
   * Update the last_session status for a specific conversation and re-sort
   * so the most recently active conversation floats to the top.
   * When a session completes or fails, ``is_read`` is reset to false so the
   * conversation moves to the "Fertig" Kanban column.
   */
  function updateConversationSession(
    workspaceId: string,
    chatId: string | null,
    sessionId: string,
    status: SessionStatus,
  ): void {
    const idx = conversations.value.findIndex(
      (c) => c.workspace_id === workspaceId && c.chat_id === chatId,
    )
    if (idx === -1) return

    const conv = conversations.value[idx]!
    if (conv.last_session && conv.last_session.id === sessionId) {
      conv.last_session.status = status
    }

    // Session finishing → mark unread so it surfaces in the "Fertig" column
    if (isSessionDone(status)) {
      conv.is_read = false
    }

    // Bubble to top by updating updated_at
    conv.updated_at = new Date().toISOString()

    // Re-sort
    conversations.value = [...conversations.value].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )
  }

  /**
   * Update workspace_status for all conversations that belong to a workspace.
   */
  function updateWorkspaceStatus(workspaceId: string, status: WorkspaceStatus): void {
    for (const conv of conversations.value) {
      if (conv.workspace_id === workspaceId) {
        conv.workspace_status = status
      }
    }
  }

  return {
    conversations,
    loading,
    error,
    searchQuery,
    filteredConversations,
    uniqueWorkspaceIds,
    markAsRead,
    markAsUnread,
    fetchConversations,
    updateConversationSession,
    updateWorkspaceStatus,
  }
})
