/**
 * Harness conversations Pinia store.
 *
 * Powers the dashboard kanban/list feed of root harness sessions.
 * Unread state is sourced from backend `last_read_at` / `manual_unread_at`.
 * Pending permission/question gates set `needs_attention`.
 * Order and displayed times use `last_message_at` (last completed
 * user or assistant message), not `updated_at`.
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
  let conversationsGeneration = 0
  let conversationsContext = localStorage.getItem('kern_active_org_id') ?? ''
  let activeFetchContext: string | null = null
  const fetchFlights = new Map<string, Promise<HarnessConversation[]>>()
  const markReadFlights = new Map<string, Promise<boolean>>()
  const markUnreadFlights = new Map<string, Promise<void>>()

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
    const context = localStorage.getItem('kern_active_org_id') ?? ''
    if (context !== conversationsContext) {
      conversationsContext = context
      conversationsGeneration += 1
      fetchFlights.clear()
      conversations.value = []
    }
    let flight = fetchFlights.get(context)
    if (!flight) {
      const generation = ++conversationsGeneration
      flight = listHarnessConversations().then((raw) => {
        if (generation === conversationsGeneration && context === (localStorage.getItem('kern_active_org_id') ?? '')) {
          conversations.value = raw.sort((a, b) => new Date(b.last_message_at).getTime() - new Date(a.last_message_at).getTime())
        }
        return raw
      }).finally(() => {
        if (fetchFlights.get(context) === flight) fetchFlights.delete(context)
      })
      fetchFlights.set(context, flight)
    }
    const generation = conversationsGeneration
    activeFetchContext = context
    loading.value = true
    error.value = null
    try {
      await flight
    } catch (e: unknown) {
      if (generation === conversationsGeneration && context === (localStorage.getItem('kern_active_org_id') ?? '')) {
        error.value = e instanceof Error ? e.message : 'Failed to load conversations'
      }
    } finally {
      if (generation === conversationsGeneration && activeFetchContext === context) {
        loading.value = false
        activeFetchContext = null
      }
    }
  }

  async function markAsRead(sessionId: string, force = false): Promise<boolean> {
    const existing = markReadFlights.get(sessionId)
    if (existing) {
      if (!force) return existing
      return existing.then((persisted) => persisted || markAsRead(sessionId, true))
    }
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    const previousUnread = conv?.unread ?? false
    const previousManual = conv?.manual_unread ?? false
    if (conv) {
      conv.unread = false
      conv.manual_unread = false
    }
    if (!force && !previousUnread && !previousManual) return true
    const flight = markHarnessSessionRead(sessionId).then(() => true).catch(() => {
      if (conv && conversations.value.includes(conv)) {
        conv.unread = previousUnread
        conv.manual_unread = previousManual
      }
      return false
    }).finally(() => {
      if (markReadFlights.get(sessionId) === flight) markReadFlights.delete(sessionId)
    })
    markReadFlights.set(sessionId, flight)
    return flight
  }

  async function markAsUnread(sessionId: string): Promise<void> {
    const existing = markUnreadFlights.get(sessionId)
    if (existing) return existing
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    const previousUnread = conv?.unread ?? false
    const previousManual = conv?.manual_unread ?? false
    if (conv) {
      conv.unread = true
      conv.manual_unread = true
    }

    const flight = markHarnessSessionUnread(sessionId).catch(() => {
      if (conv && conversations.value.includes(conv)) {
        conv.unread = previousUnread
        conv.manual_unread = previousManual
      }
    }).finally(() => {
      if (markUnreadFlights.get(sessionId) === flight) markUnreadFlights.delete(sessionId)
    })
    markUnreadFlights.set(sessionId, flight)
    return flight
  }

  function updateSessionStatus(
    sessionId: string,
    status: HarnessSessionStatus,
    viewed = false,
  ): void {
    const conv = conversations.value.find((row) => row.session_id === sessionId)
    if (!conv) return
    if (conv.status !== status) {
      conv.last_message_at = new Date().toISOString()
    }
    conv.status = status
    if (!conv.manual_unread) {
      if (status === 'idle') {
        conv.unread = !viewed
      } else {
        conv.unread = false
      }
    }
    conversations.value = [...conversations.value].sort(
      (a, b) =>
        new Date(b.last_message_at).getTime() - new Date(a.last_message_at).getTime(),
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
    setAttention,
    clearAttention,
    scheduleAttentionRefresh,
  }
})

function mergeAttentionKind(
  current: HarnessAttentionKind | undefined,
  next: 'permission' | 'question',
): HarnessAttentionKind {
  if (!current || current === next) return next
  return 'both'
}
