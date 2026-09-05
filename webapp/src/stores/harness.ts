/**
 * Harness Pinia store.
 *
 * Owns the harness block model (`HarnessSession` + `HarnessMessage` with
 * `parts[]` + todos + pending permission requests). All streaming
 * transitions delegate to the pure reducers in `lib/harnessReducer.ts`.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type {
  HarnessMessage,
  HarnessPartDelta,
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessSession,
  HarnessSessionMode,
  HarnessTodo,
} from '@/types/harness'
import {
  abortHarnessSession,
  createHarnessSession,
  listHarnessParts,
  listHarnessSessions,
  listHarnessTodos,
  resolveHarnessPermission,
  sendHarnessMessage,
} from '@/services/harness.api'
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureAssistantMessage,
} from '@/lib/harnessReducer'
import { useNotificationStore } from './notifications'

export const useHarnessStore = defineStore('harness', () => {
  // --- State ---
  const sessions = ref<HarnessSession[]>([])
  /** Messages keyed by session id. */
  const messagesBySession = ref<Record<string, HarnessMessage[]>>({})
  /** Todos keyed by session id. */
  const todosBySession = ref<Record<string, HarnessTodo[]>>({})
  /** Pending permission requests keyed by request id. */
  const pendingPermissions = ref<Record<string, HarnessPermissionRequest>>({})
  const activeSessionId = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** Model picker value when no ProviderConfig REST endpoint exists. */
  const modelInput = ref('')

  // --- Getters ---
  const activeSession = computed(
    () => sessions.value.find((s) => s.id === activeSessionId.value) ?? null,
  )

  const activeMessages = computed<HarnessMessage[]>(
    () => (activeSessionId.value ? (messagesBySession.value[activeSessionId.value] ?? []) : []),
  )

  const activeTodos = computed<HarnessTodo[]>(
    () => (activeSessionId.value ? (todosBySession.value[activeSessionId.value] ?? []) : []),
  )

  const activePermissionRequests = computed<HarnessPermissionRequest[]>(() =>
    Object.values(pendingPermissions.value).filter(
      (request) => request.session_id === activeSessionId.value,
    ),
  )

  function messagesFor(sessionId: string): HarnessMessage[] {
    if (!messagesBySession.value[sessionId]) {
      messagesBySession.value[sessionId] = []
    }
    return messagesBySession.value[sessionId]!
  }

  // --- Actions ---

  async function fetchSessions(workspaceId: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      sessions.value = await listHarnessSessions(workspaceId)
      if (!activeSessionId.value && sessions.value.length > 0) {
        activeSessionId.value = sessions.value[0]!.id
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load harness sessions'
    } finally {
      loading.value = false
    }
  }

  function setActiveSession(sessionId: string | null): void {
    activeSessionId.value = sessionId
  }

  async function fetchParts(sessionId: string): Promise<void> {
    try {
      const response = await listHarnessParts(sessionId)
      messagesBySession.value[sessionId] = response.messages.map((message) => ({
        ...message,
        session_id: sessionId,
        parts: message.parts ?? [],
      }))
    } catch (e: unknown) {
      const notifications = useNotificationStore()
      notifications.error(
        'Failed to load messages',
        e instanceof Error ? e.message : 'Unknown error',
      )
    }
  }

  async function fetchTodos(sessionId: string): Promise<void> {
    try {
      todosBySession.value[sessionId] = await listHarnessTodos(sessionId)
    } catch {
      todosBySession.value[sessionId] = []
    }
  }

  async function createSession(
    workspaceId: string,
    prompt: string,
    mode: HarnessSessionMode,
    model: string,
  ): Promise<HarnessSession | null> {
    const notifications = useNotificationStore()
    try {
      const session = await createHarnessSession(workspaceId, {
        prompt,
        mode,
        model,
        agent_name: mode,
      })
      sessions.value.unshift(session)
      activeSessionId.value = session.id
      messagesBySession.value[session.id] = [
        {
          id: `local-user-${session.id}`,
          session_id: session.id,
          role: 'user',
          content: prompt,
          parts: [],
        },
      ]
      await fetchParts(session.id)
      return session
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  async function sendMessage(sessionId: string, prompt: string): Promise<void> {
    const notifications = useNotificationStore()
    try {
      const session = await sendHarnessMessage(sessionId, prompt)
      upsertSession(session)
      messagesFor(sessionId).push({
        id: `local-user-${sessionId}-${Date.now()}`,
        session_id: sessionId,
        role: 'user',
        content: prompt,
        parts: [],
      })
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function abortSession(sessionId: string): Promise<void> {
    const notifications = useNotificationStore()
    try {
      const session = await abortHarnessSession(sessionId)
      upsertSession(session)
    } catch (e: unknown) {
      notifications.error('Abort failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  function upsertSession(session: HarnessSession): void {
    const idx = sessions.value.findIndex((s) => s.id === session.id)
    if (idx === -1) {
      sessions.value.unshift(session)
    } else {
      sessions.value[idx] = session
    }
  }

  // --- Real-time reducers (called from socket handlers) ---

  function handlePartUpdated(
    sessionId: string,
    delta: HarnessPartDelta,
    opts: { step?: number; partId?: string } = {},
  ): void {
    applyPartDelta(messagesFor(sessionId), sessionId, delta, {
      step: opts.step,
      partId: opts.partId,
    })
  }

  function handleTodoUpdated(sessionId: string, todos: HarnessTodo[]): void {
    todosBySession.value[sessionId] = applyTodoUpdate(
      todosBySession.value[sessionId] ?? [],
      { todos },
    )
  }

  function handleSubtaskStarted(
    sessionId: string,
    event: { subtask_id: string; agent: string; description: string; part_id?: string },
  ): void {
    const message = ensureAssistantMessage(messagesFor(sessionId), sessionId)
    applySubtaskStarted(message, sessionId, {
      workspace_id: '',
      session_id: sessionId,
      subtask_id: event.subtask_id,
      agent: event.agent,
      description: event.description,
      part_id: event.part_id,
    })
  }

  function handleSubtaskFinished(
    sessionId: string,
    event: { subtask_id: string; agent?: string; status: string; summary: string },
  ): void {
    const message = ensureAssistantMessage(messagesFor(sessionId), sessionId)
    applySubtaskFinished(message, {
      workspace_id: '',
      session_id: sessionId,
      subtask_id: event.subtask_id,
      agent: event.agent,
      status: event.status,
      summary: event.summary,
    })
  }

  function handleSessionStatus(sessionId: string, status: HarnessSession['status']): void {
    const session = sessions.value.find((s) => s.id === sessionId)
    if (session) session.status = status
  }

  function handlePermissionRequired(request: HarnessPermissionRequest): void {
    pendingPermissions.value[request.request_id] = { ...request, status: 'pending' }
  }

  function handlePermissionResolved(requestId: string, decision: string): void {
    const pending = pendingPermissions.value[requestId]
    if (!pending) return
    pending.status = decision === 'reject' ? 'rejected' : 'approved'
    delete pendingPermissions.value[requestId]
  }

  async function resolvePermission(
    sessionId: string,
    requestId: string,
    response: HarnessPermissionResponse,
  ): Promise<void> {
    const notifications = useNotificationStore()
    const request = pendingPermissions.value[requestId]
    if (!request) return
    try {
      const outcome = await resolveHarnessPermission(sessionId, request, response)
      handlePermissionResolved(requestId, outcome.decision)
    } catch (e: unknown) {
      notifications.error(
        'Permission failed',
        e instanceof Error ? e.message : 'Unknown error',
      )
    }
  }

  function reset(): void {
    sessions.value = []
    messagesBySession.value = {}
    todosBySession.value = {}
    pendingPermissions.value = {}
    activeSessionId.value = null
    loading.value = false
    error.value = null
  }

  return {
    // State
    sessions,
    messagesBySession,
    todosBySession,
    pendingPermissions,
    activeSessionId,
    loading,
    error,
    modelInput,
    // Getters
    activeSession,
    activeMessages,
    activeTodos,
    activePermissionRequests,
    // Actions
    fetchSessions,
    setActiveSession,
    fetchParts,
    fetchTodos,
    createSession,
    sendMessage,
    abortSession,
    resolvePermission,
    // Real-time
    messagesFor,
    handlePartUpdated,
    handleTodoUpdated,
    handleSubtaskStarted,
    handleSubtaskFinished,
    handleSessionStatus,
    handlePermissionRequired,
    handlePermissionResolved,
    reset,
  }
})
