/**
 * Harness Pinia store.
 *
 * Owns the harness block model (`HarnessSession` + `HarnessMessage` with
 * `parts[]` + todos + pending permission requests). All streaming
 * transitions delegate to the pure reducers in `@opencuria/chat-core/harnessReducer`.
 */

import { defineStore } from 'pinia'
import { computed, ref, toRaw } from 'vue'

import type {
  HarnessMessage,
  HarnessPartDelta,
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSessionMode,
  HarnessId,
  HarnessSteer,
  HarnessTodo,
} from '@opencuria/chat-core/harness'
import {
  abortHarnessSession,
  createHarnessSession,
  deleteHarnessSession,
  dismissHarnessNotice,
  editHarnessMessage,
  forkHarnessSession,
  getHarnessPart,
  listHarnessParts,
  listHarnessSessions,
  listHarnessTodos,
  patchHarnessSession,
  resolveHarnessPermission,
  resolveHarnessQuestion,
  sendHarnessMessage,
  setSessionMode,
  steerHarnessSession,
  withdrawHarnessSteer,
} from '@/services/harness.api'
import { ApiRequestError } from '@/services/api'
import {
  ChatTimelineController,
  StaleChatRequestError,
} from '@opencuria/chat-core/chatTimelineController'
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureBusyAssistant,
  hasDeliveredSteer,
  routeAssistantMessage,
  sortHarnessParts,
} from '@opencuria/chat-core/harnessReducer'
import {
  collectAncestorSessions,
  collectDescendantSessionIds,
  collectRunningChildSessionIds,
} from '@opencuria/chat-core/harnessSubtaskActivity'
import { loadAgentConfigsCached } from '@/lib/agentConfigs'
import { recordRecentModelUsage } from '@/lib/recentModels'
import type { AgentConfig } from '@opencuria/chat-core/harnessAgents'
import {
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from '@opencuria/chat-core/harnessModels'
import { clearQuestionInputCache, pruneQuestionInputCaches } from '@/lib/questionInputCache'
import { useAuthStore } from './auth'
import { useWorkspaceStore } from './workspaces'
import { useNotificationStore } from './notifications'
import { useHarnessConversationStore } from './harnessConversations'

/** Run options of a new user turn (a steer falls back to them when the run ended). */
interface SendOptions {
  mode?: HarnessSessionMode
  model?: string
  skillIds?: string[]
  reasoningEffort?: string
}

/** External defaults are resolved server-side; remember the admitted selection. */
function recordComposerModelUsage(
  session: HarnessSession,
  model: string,
  effort: string,
  harnessId = session.harness_id ?? 'native',
): void {
  recordRecentModelUsage(
    session.model || model,
    session.reasoning_effort ?? effort,
    harnessId,
    session.mode,
  )
}

export const useHarnessStore = defineStore('harness', () => {
  // --- State ---
  const sessions = ref<HarnessSession[]>([])
  /** Messages keyed by session id. */
  const messagesBySession = ref<Record<string, HarnessMessage[]>>({})
  /** Todos keyed by session id. */
  const todosBySession = ref<Record<string, HarnessTodo[]>>({})
  /** Pending permission requests keyed by request id. */
  const pendingPermissions = ref<Record<string, HarnessPermissionRequest>>({})
  /** Pending question requests keyed by request id. */
  const pendingQuestions = ref<Record<string, HarnessQuestionRequest>>({})
  /** Queued (not yet delivered) steers keyed by session id, FIFO. */
  const queuedSteers = ref<Record<string, HarnessSteer[]>>({})
  const stoppingSessionIds = ref<string[]>([])
  const activeSessionId = ref<string | null>(null)
  /** Session currently open in the chat panel; cleared when the panel unmounts. */
  const viewingSessionId = ref<string | null>(null)
  const loading = ref(false)
  /** True once the initial workspace session list has resolved. */
  const sessionsLoaded = ref(false)
  /** Workspace whose initial session load completed (guards skeleton flash). */
  const sessionsLoadedWorkspace = ref<string | null>(null)
  const error = ref<string | null>(null)
  // Composer selection; empty means "no selection yet" (placeholder).
  const modelInput = ref('')
  // Reasoning effort for the next run; empty uses the model default.
  const effortInput = ref('')
  // Cached agent configs backing mode defaults.
  const agentConfigs = ref<AgentConfig[]>([])
  // True once the user manually changed model or effort.
  const composerDirty = ref(false)
  /** Message ids whose error/abort notice the user dismissed (optimistic local cache; server `notice_dismissed_at` is the source of truth). */
  const dismissedNoticeIds = ref<Record<string, true>>({})

  const sessionFetches = new Map<string, Promise<void>>()
  const todoFetches = new Map<string, Promise<void>>()
  /** Deleted edit suffixes must not be resurrected by stale snapshots or events. */
  const discardedMessageIds = new Map<string, Set<string>>()
  const detailErrors = ref<Record<string, string>>({})
  let activeSelectionGeneration = 0
  const todoGenerations = new Map<string, number>()
  let sessionsGeneration = 0
  let activeSessionsWorkspace: string | null = null
  const readFlights = new Map<string, Promise<boolean>>()
  /**
   * Bumped whenever a session's steer queue changes. Responses that were in
   * flight while a newer socket snapshot landed must not overwrite it.
   */
  const steerRevisions = new Map<string, number>()
  /** Sessions with a user-initiated turn request in flight (they fetch themselves). */
  const userTurnRequests = new Map<string, number>()

  // --- Getters ---
  const activeSession = computed(
    () => sessions.value.find((s) => s.id === activeSessionId.value) ?? null,
  )

  const activeMessages = computed<HarnessMessage[]>(() =>
    activeSessionId.value ? (messagesBySession.value[activeSessionId.value] ?? []) : [],
  )

  const activeTodos = computed<HarnessTodo[]>(() =>
    activeSessionId.value ? (todosBySession.value[activeSessionId.value] ?? []) : [],
  )

  const activeQueuedSteers = computed<HarnessSteer[]>(() =>
    activeSessionId.value ? (queuedSteers.value[activeSessionId.value] ?? []) : [],
  )

  const activeLineageIds = computed(() => {
    if (!activeSessionId.value) return new Set<string>()
    return new Set(collectDescendantSessionIds(activeSessionId.value, sessions.value))
  })

  const activePermissionRequests = computed<HarnessPermissionRequest[]>(() =>
    Object.values(pendingPermissions.value)
      .filter((request) => activeLineageIds.value.has(request.session_id))
      .map((request) => enrichGateAgent(request)),
  )

  const activeQuestionRequests = computed<HarnessQuestionRequest[]>(() =>
    Object.values(pendingQuestions.value)
      .filter((request) => activeLineageIds.value.has(request.session_id))
      .map((request) => enrichGateAgent(request)),
  )

  function enrichGateAgent<T extends { session_id: string; agent_name?: string }>(request: T): T {
    if (request.agent_name) return request
    const session = sessions.value.find((item) => item.id === request.session_id)
    if (!session?.agent_name) return request
    return { ...request, agent_name: session.agent_name }
  }

  /** Root → current session chain for the header breadcrumb. */
  const activeSessionLineage = computed<HarnessSession[]>(() => {
    if (!activeSessionId.value) return []
    return collectAncestorSessions(activeSessionId.value, sessions.value)
  })

  function messagesFor(sessionId: string): HarnessMessage[] {
    if (!messagesBySession.value[sessionId]) {
      messagesBySession.value[sessionId] = []
    }
    return messagesBySession.value[sessionId]!
  }

  /** Keep Vue row references reactive while the shared controller replaces snapshots. */
  function publishTranscript(sessionId: string, messages: HarnessMessage[]): void {
    const current = new Map(
      (messagesBySession.value[sessionId] ?? []).map((message) => [message.id, message]),
    )
    messagesBySession.value[sessionId] = messages.map((message) => {
      const existing = current.get(message.id)
      if (!existing || existing === message) return message
      const parts = new Map(existing.parts.map((part) => [part.id, part]))
      const publishedParts = message.parts.map((part) => {
        const previous = parts.get(part.id)
        return previous ? Object.assign(previous, part) : part
      })
      return Object.assign(existing, message, { parts: publishedParts })
    })
  }

  const timeline = new ChatTimelineController({
    load: listHarnessParts,
    loadDetail: getHarnessPart,
    read: (sessionId) => messagesBySession.value[sessionId],
    write: publishTranscript,
    sessions: () => sessions.value,
    discardMessage: isDiscardedMessage,
    retainedSessionIds: retainedTranscriptSessionIds,
    forget: forgetTranscript,
    unwrap: toRaw,
    onError: (cause) =>
      useNotificationStore().error(
        'Failed to load messages',
        cause instanceof Error ? cause.message : 'Unknown error',
      ),
  })

  function touchTranscript(sessionId: string, recalculateBytes = false): void {
    timeline.touch(sessionId, recalculateBytes)
  }

  /** Selected, viewed, gated and todo-loading sessions; the timeline owns the policy. */
  function retainedTranscriptSessionIds(): Array<string | null> {
    return [
      activeSessionId.value,
      viewingSessionId.value,
      ...Object.values(pendingPermissions.value).map((request) => request.session_id),
      ...Object.values(pendingQuestions.value).map((request) => request.session_id),
      ...todoFetches.keys(),
    ]
  }

  function invalidatePartFetches(sessionId: string): void {
    timeline.invalidate(sessionId)
  }

  function forgetTranscript(sessionId: string): void {
    timeline.forget(sessionId)
    discardedMessageIds.delete(sessionId)
    invalidatePartFetches(sessionId)
    todoGenerations.set(sessionId, (todoGenerations.get(sessionId) ?? 0) + 1)
    todoFetches.delete(sessionId)
    const errors = { ...detailErrors.value }
    for (const key of Object.keys(errors)) {
      if (key.startsWith(`${sessionId}:`)) delete errors[key]
    }
    detailErrors.value = errors
    const dismissed = { ...dismissedNoticeIds.value }
    for (const message of messagesBySession.value[sessionId] ?? []) {
      delete dismissed[message.id]
    }
    dismissedNoticeIds.value = dismissed
    delete messagesBySession.value[sessionId]
    delete todosBySession.value[sessionId]
    delete queuedSteers.value[sessionId]
    steerRevisions.delete(sessionId)
  }

  function trimInactiveTranscripts(): void {
    timeline.trim()
  }

  /** Clear transcript caches and invalidate all outstanding session fetches. */
  function clearTranscriptCaches(): void {
    const todoSessionIds = new Set([...todoGenerations.keys(), ...todoFetches.keys()])
    for (const sessionId of todoSessionIds) {
      todoGenerations.set(sessionId, (todoGenerations.get(sessionId) ?? 0) + 1)
    }
    timeline.clear()
    discardedMessageIds.clear()
    todoFetches.clear()
    detailErrors.value = {}
    messagesBySession.value = {}
    todosBySession.value = {}
    pendingPermissions.value = {}
    pendingQuestions.value = {}
    queuedSteers.value = {}
    steerRevisions.clear()
    dismissedNoticeIds.value = {}
    readFlights.clear()
  }

  // --- Actions ---

  function pinPartDetail(sessionId: string, partId: string, pinned: boolean): void {
    timeline.pinDetail(sessionId, partId, pinned)
  }

  function invalidatePartDetails(sessionId: string, clear = false): void {
    timeline.invalidateDetails(sessionId, clear, clear)
  }

  async function fetchPartDetail(sessionId: string, partId: string): Promise<boolean> {
    const part = messagesBySession.value[sessionId]
      ?.flatMap((message) => message.parts)
      .find((item) => item.id === partId)
    if (!part || part.detail_loaded) return Boolean(part?.detail_loaded)
    if (part.state === 'pending' || part.state === 'running') return false
    const selection = activeSelectionGeneration
    const generation = timeline.generation(sessionId)
    const isCurrent = () =>
      activeSessionId.value === sessionId &&
      selection === activeSelectionGeneration &&
      generation === timeline.generation(sessionId) &&
      sessions.value.some((session) => session.id === sessionId)
    const key = `${sessionId}:${partId}`
    try {
      await timeline.requestDetail(sessionId, partId, { context: String(selection), isCurrent })
      const errors = { ...detailErrors.value }
      delete errors[key]
      detailErrors.value = errors
      return true
    } catch (cause) {
      if (!isCurrent() || cause instanceof StaleChatRequestError) return false
      detailErrors.value = {
        ...detailErrors.value,
        [key]: cause instanceof Error ? cause.message : 'Failed to load part details',
      }
      throw cause
    }
  }

  async function fetchSessions(workspaceId: string): Promise<void> {
    if (activeSessionsWorkspace !== workspaceId) {
      const isWorkspaceSwitch = activeSessionsWorkspace !== null
      activeSessionsWorkspace = workspaceId
      sessionsGeneration += 1
      sessionFetches.clear()
      if (isWorkspaceSwitch) {
        clearTranscriptCaches()
        sessions.value = []
        setActiveSession(null)
        viewingSessionId.value = null
        // A new workspace needs its own initial load; the chat must not
        // flash the previous workspace's skeleton state.
        sessionsLoaded.value = false
        sessionsLoadedWorkspace.value = null
      }
    }
    let flight = sessionFetches.get(workspaceId)
    if (!flight) {
      const generation = ++sessionsGeneration
      flight = listHarnessSessions(workspaceId)
        .then((result) => {
          if (generation !== sessionsGeneration) return
          const previous = sessions.value
          sessions.value = result
          const sessionIds = new Set(result.map((session) => session.id))
          for (const session of previous) {
            if (!sessionIds.has(session.id)) forgetTranscript(session.id)
          }
          if (
            activeSessionId.value &&
            !result.some((session) => session.id === activeSessionId.value) &&
            previous.some((session) => session.id === activeSessionId.value)
          )
            setActiveSession(null)
        })
        .catch((e: unknown) => {
          if (generation === sessionsGeneration) {
            error.value = e instanceof Error ? e.message : 'Failed to load harness sessions'
          }
        })
        .finally(() => {
          if (sessionFetches.get(workspaceId) === flight) sessionFetches.delete(workspaceId)
        })
      sessionFetches.set(workspaceId, flight)
    }
    const generation = sessionsGeneration
    // `loading` gates the chat skeleton: only the very first workspace
    // load may show it. Same-workspace refreshes (e.g. subagent start
    // calling fetchSessions) must never hide rendered chat behind
    // skeletons.
    const isInitialLoad =
      sessionsLoadedWorkspace.value !== workspaceId && messagesForAnySessionEmpty()
    if (isInitialLoad) loading.value = true
    error.value = null
    await flight
    if (generation === sessionsGeneration) {
      if (activeSessionsWorkspace === workspaceId) {
        sessionsLoaded.value = true
        sessionsLoadedWorkspace.value = workspaceId
      }
      loading.value = false
    }
  }

  function messagesForAnySessionEmpty(): boolean {
    if (activeSessionId.value) {
      return (messagesBySession.value[activeSessionId.value] ?? []).length === 0
    }
    return Object.keys(messagesBySession.value).length === 0
  }

  function setActiveSession(sessionId: string | null): void {
    if (activeSessionId.value !== sessionId) {
      const previous = activeSessionId.value
      if (previous) touchTranscript(previous)
      activeSelectionGeneration += 1
    }
    activeSessionId.value = sessionId
    if (sessionId && messagesBySession.value[sessionId]) touchTranscript(sessionId)
    trimInactiveTranscripts()
  }

  function setViewingSession(sessionId: string | null): void {
    viewingSessionId.value = sessionId
    if (sessionId && messagesBySession.value[sessionId]) touchTranscript(sessionId)
    trimInactiveTranscripts()
    if (!sessionId) return
    const session = sessions.value.find((row) => row.id === sessionId)
    if (session?.status === 'idle' && !session.manual_unread && session.unread) {
      void markSessionRead(sessionId)
    }
  }

  async function markSessionRead(sessionId: string, force = false): Promise<void> {
    const existing = readFlights.get(sessionId)
    if (existing) {
      if (!force) {
        await existing
        return
      }
      const persisted = await existing
      if (!persisted) await markSessionRead(sessionId, true)
      return
    }
    const session = sessions.value.find((row) => row.id === sessionId)
    const conversationStore = useHarnessConversationStore()
    const conv = conversationStore.conversations.find((row) => row.session_id === sessionId)
    const needsPersist = Boolean(
      force || session?.unread || session?.manual_unread || conv?.unread || conv?.manual_unread,
    )
    const previousUnread = session?.unread ?? false
    const previousManualUnread = session?.manual_unread ?? false
    if (session) {
      session.unread = false
      session.manual_unread = false
    }
    const flight = (async () => {
      const persisted = !needsPersist || (await conversationStore.markAsRead(sessionId, true))
      if (!persisted && session && sessions.value.includes(session)) {
        session.unread = previousUnread
        session.manual_unread = previousManualUnread
      }
      return persisted
    })().finally(() => {
      if (readFlights.get(sessionId) === flight) readFlights.delete(sessionId)
    })
    readFlights.set(sessionId, flight)
    await flight
  }

  async function fetchParts(
    sessionId: string,
    hydrateChildren = true,
    refreshAfterExisting = false,
  ): Promise<void> {
    await timeline.fetch(sessionId, {
      variant: String(hydrateChildren),
      refreshAfterExisting,
      prepare: () => {
        const auth = useAuthStore()
        const identity = { userId: auth.user?.id, organizationId: auth.activeOrganizationId }
        const steerRevision = steerRevisions.get(sessionId) ?? 0
        const knownGateIds = new Set([
          ...Object.keys(pendingPermissions.value),
          ...Object.keys(pendingQuestions.value),
        ])
        return async (response) => {
          // Pending gates are listed for this session and all descendants, so
          // the response also settles subagent gates resolved while offline.
          const scope = new Set(collectDescendantSessionIds(sessionId, sessions.value))
          const settled = (id: string, request: { session_id: string } | undefined) =>
            knownGateIds.has(id) && !!request && scope.has(request.session_id)
          const nextPermissions = { ...pendingPermissions.value }
          for (const id of Object.keys(nextPermissions)) {
            if (settled(id, nextPermissions[id])) delete nextPermissions[id]
          }
          for (const request of response.permissions ?? []) {
            nextPermissions[request.request_id] = { ...request, status: 'pending' }
          }
          pendingPermissions.value = nextPermissions
          const nextQuestions = { ...pendingQuestions.value }
          for (const id of Object.keys(nextQuestions)) {
            if (settled(id, nextQuestions[id])) delete nextQuestions[id]
          }
          for (const request of response.questions ?? []) {
            nextQuestions[request.request_id] = { ...request, status: 'pending' }
          }
          pendingQuestions.value = nextQuestions
          for (const scopedSessionId of scope) {
            pruneQuestionInputCaches(
              identity,
              { workspace_id: response.session.workspace_id, session_id: scopedSessionId },
              Object.keys(nextQuestions),
            )
          }
          // A socket snapshot that landed while this request was in flight is
          // newer than the response; an absent field (older backend) is no news.
          if (response.steers && (steerRevisions.get(sessionId) ?? 0) === steerRevision) {
            setQueuedSteers(sessionId, response.steers)
          }
          if (hydrateChildren) {
            const childIds = collectRunningChildSessionIds(messagesBySession.value[sessionId] ?? [])
            await Promise.all(childIds.map((childId) => fetchParts(childId, false)))
          }
        }
      },
    })
  }

  async function refreshPartsAfterIdle(sessionId: string): Promise<void> {
    await fetchParts(sessionId, true, true)
  }

  async function fetchTodos(sessionId: string): Promise<void> {
    let flight = todoFetches.get(sessionId)
    if (!flight) {
      const generation = (todoGenerations.get(sessionId) ?? 0) + 1
      todoGenerations.set(sessionId, generation)
      flight = listHarnessTodos(sessionId)
        .then((todos) => {
          if (todoGenerations.get(sessionId) === generation) todosBySession.value[sessionId] = todos
        })
        .catch(() => {
          if (todoGenerations.get(sessionId) === generation) todosBySession.value[sessionId] = []
        })
        .finally(() => {
          if (todoFetches.get(sessionId) === flight) {
            todoFetches.delete(sessionId)
            trimInactiveTranscripts()
          }
        })
      todoFetches.set(sessionId, flight)
    }
    await flight
  }

  function sessionTransitioning(sessionId: string): boolean {
    const session = sessions.value.find((item) => item.id === sessionId)
    return Boolean(session && useWorkspaceStore().isWorkspaceTransitioning(session.workspace_id))
  }

  async function createSession(
    workspaceId: string,
    prompt: string,
    mode: HarnessSessionMode,
    model: string,
    skillIds: string[] = [],
    reasoningEffort = '',
    harnessId: HarnessId = 'native',
  ): Promise<HarnessSession | null> {
    if (useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return null
    const notifications = useNotificationStore()
    try {
      const session = await createHarnessSession(workspaceId, {
        prompt,
        mode,
        model,
        agent_name: mode,
        skill_ids: skillIds,
        reasoning_effort: reasoningEffort,
        harness_id: harnessId,
      })
      sessions.value.unshift(session)
      setActiveSession(session.id)
      messagesBySession.value[session.id] = [
        {
          id: `local-user-${session.id}`,
          session_id: session.id,
          role: 'user',
          content: prompt,
          skill_ids: [...skillIds],
          parts: [],
          created_at: new Date().toISOString(),
        },
      ]
      await fetchParts(session.id)
      recordComposerModelUsage(session, model, reasoningEffort, harnessId)
      return session
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  /** Mark a user-initiated turn request so the matching busy event skips its own fetch. */
  async function trackUserTurn<T>(sessionId: string, request: () => Promise<T>): Promise<T> {
    userTurnRequests.set(sessionId, (userTurnRequests.get(sessionId) ?? 0) + 1)
    try {
      return await request()
    } finally {
      const remaining = (userTurnRequests.get(sessionId) ?? 1) - 1
      if (remaining > 0) userTurnRequests.set(sessionId, remaining)
      else userTurnRequests.delete(sessionId)
    }
  }

  /**
   * A run can start without this client asking for it (the follow-up of a
   * queued steer, another tab, a schedule). Load the transcript when the
   * user message that opened the run is unknown locally.
   */
  async function ensureUserTurnLoaded(sessionId: string, userMessageId: string): Promise<void> {
    if (userTurnRequests.has(sessionId) || isDiscardedMessage(sessionId, userMessageId)) return
    if (!sessions.value.some((item) => item.id === sessionId)) return
    const messages = messagesBySession.value[sessionId] ?? []
    if (messages.some((message) => message.id === userMessageId)) return
    await fetchParts(sessionId)
  }

  /** Send a follow-up message; resolves `true` once the server accepted it. */
  function sendMessage(
    sessionId: string,
    prompt: string,
    options: SendOptions = {},
  ): Promise<boolean> {
    return trackUserTurn(sessionId, () => sendMessageRequest(sessionId, prompt, options))
  }

  async function sendMessageRequest(
    sessionId: string,
    prompt: string,
    options: SendOptions,
  ): Promise<boolean> {
    if (sessionTransitioning(sessionId)) return false
    const notifications = useNotificationStore()
    try {
      const previousSession = sessions.value.find((item) => item.id === sessionId)
      const session = await sendHarnessMessage(sessionId, {
        prompt,
        mode: options.mode,
        model: options.model,
        skill_ids: options.skillIds,
        reasoning_effort: options.reasoningEffort,
      })
      upsertSession(session)
      // The send endpoint persists both the user and the fresh assistant
      // before returning. Fetch their authoritative ids/positions instead
      // of inventing a local user that can collide with an identical prompt.
      await fetchParts(sessionId)
      recordComposerModelUsage(
        session,
        options.model ?? '',
        options.reasoningEffort ?? '',
        session.harness_id ?? previousSession?.harness_id,
      )
      return true
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
      return false
    }
  }

  /** Replace a session's steer queue (idempotent snapshot semantics). */
  function setQueuedSteers(sessionId: string, steers: HarnessSteer[]): void {
    steerRevisions.set(sessionId, (steerRevisions.get(sessionId) ?? 0) + 1)
    if (steers.length === 0) delete queuedSteers.value[sessionId]
    else queuedSteers.value[sessionId] = steers.map((steer) => ({ ...steer }))
  }

  /**
   * Send a message into the running turn. It is queued server-side and flows
   * into the run at the next step boundary. Resolves `true` when the message
   * was accepted (queued, or sent as a normal prompt because the run had just
   * ended) and `false` when it failed so the composer can keep the text.
   */
  async function steerSession(
    sessionId: string,
    prompt: string,
    fallback: SendOptions = {},
  ): Promise<boolean> {
    if (sessionTransitioning(sessionId)) return false
    const notifications = useNotificationStore()
    const revision = steerRevisions.get(sessionId) ?? 0
    try {
      const steer = await steerHarnessSession(sessionId, prompt)
      const queue = queuedSteers.value[sessionId] ?? []
      // The socket snapshot normally lands first; only fill the gap. A newer
      // snapshot (delivery, discard, abort) or an already delivered steer
      // must never be resurrected by this late response.
      if (
        revision === (steerRevisions.get(sessionId) ?? 0) &&
        sessions.value.some((item) => item.id === sessionId) &&
        !queue.some((item) => item.id === steer.id) &&
        !hasDeliveredSteer(messagesBySession.value[sessionId] ?? [], steer.id)
      ) {
        setQueuedSteers(sessionId, [...queue, steer])
      }
      return true
    } catch (e: unknown) {
      // The run ended between the click and the request: send a regular turn.
      if (e instanceof ApiRequestError && e.status === 409 && e.code === 'no_active_run') {
        return sendMessage(sessionId, prompt, fallback)
      }
      notifications.error('Steer failed', e instanceof Error ? e.message : 'Unknown error')
      return false
    }
  }

  /**
   * Withdraw a queued steer. Resolves with its text to put back into the
   * composer, or `null` when it was already delivered (or the call failed).
   */
  async function withdrawSteer(sessionId: string, steerId: string): Promise<string | null> {
    if (sessionTransitioning(sessionId)) return null
    const notifications = useNotificationStore()
    const dropLocal = (): void => {
      const queue = queuedSteers.value[sessionId] ?? []
      if (queue.some((item) => item.id === steerId)) {
        setQueuedSteers(
          sessionId,
          queue.filter((item) => item.id !== steerId),
        )
      }
    }
    try {
      const steer = await withdrawHarnessSteer(sessionId, steerId)
      dropLocal()
      return steer.content
    } catch (e: unknown) {
      if (e instanceof ApiRequestError && e.status === 404) {
        // Delivered in the meantime: it is part of the run now, not queued.
        dropLocal()
        return null
      }
      notifications.error('Withdraw failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  async function renameSession(sessionId: string, title: string): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    try {
      const updated = await patchHarnessSession(sessionId, { title })
      upsertSession(updated)
    } catch (e: unknown) {
      notifications.error('Rename failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function removeSession(sessionId: string): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    try {
      await deleteHarnessSession(sessionId)
      invalidatePartDetails(sessionId, true)
      forgetTranscript(sessionId)
      sessions.value = sessions.value.filter((session) => session.id !== sessionId)
      if (activeSessionId.value === sessionId) {
        setActiveSession(null)
      }
    } catch (e: unknown) {
      notifications.error('Delete failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function updateSessionMode(sessionId: string, mode: HarnessSessionMode): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    try {
      const updated = await setSessionMode(sessionId, mode)
      upsertSession(updated)
    } catch (e: unknown) {
      notifications.error('Mode change failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  /**
   * Stop the running turn. The server drops queued steers on abort; the texts
   * that were queued (and not delivered meanwhile) are returned so the
   * composer can restore them. Resolves empty when the abort failed.
   */
  async function abortSession(sessionId: string): Promise<string[]> {
    if (sessionTransitioning(sessionId) || stoppingSessionIds.value.includes(sessionId)) return []
    const notifications = useNotificationStore()
    const queued = [...(queuedSteers.value[sessionId] ?? [])]
    stoppingSessionIds.value = [...stoppingSessionIds.value, sessionId]
    try {
      const session = await abortHarnessSession(sessionId)
      upsertSession(session)
      if (queuedSteers.value[sessionId]) setQueuedSteers(sessionId, [])
      const messages = messagesBySession.value[sessionId] ?? []
      return queued
        .filter((steer) => !hasDeliveredSteer(messages, steer.id))
        .map((steer) => steer.content)
    } catch (e: unknown) {
      notifications.error('Abort failed', e instanceof Error ? e.message : 'Unknown error')
      return []
    } finally {
      stoppingSessionIds.value = stoppingSessionIds.value.filter((id) => id !== sessionId)
    }
  }

  /** Copy through an assistant answer, or the full session, without starting a run. */
  async function forkSession(
    sessionId: string,
    messageId?: string,
  ): Promise<HarnessSession | null> {
    if (sessionTransitioning(sessionId)) return null
    const notifications = useNotificationStore()
    try {
      const session = await forkHarnessSession(
        sessionId,
        messageId ? { message_id: messageId } : {},
      )
      upsertSession(session)
      setActiveSession(session.id)
      await fetchParts(session.id)
      void useHarnessConversationStore().fetchConversations()
      return session
    } catch (e: unknown) {
      notifications.error('Fork failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  /**
   * Replace the edited suffix immediately, then reconcile the fresh server ids.
   * Normal busy merges preserve missing rows, so explicitly retire the deleted
   * suffix and invalidate Stop's in-flight fetches before admitting the new run.
   */
  async function editMessage(
    sessionId: string,
    messageId: string,
    prompt: string,
    options: {
      mode?: HarnessSessionMode
      model?: string
      skillIds?: string[]
      reasoningEffort?: string
    } = {},
  ): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    await trackUserTurn(sessionId, async () => {
      const previous = messagesBySession.value[sessionId]
      const cutoff = previous?.findIndex((message) => message.id === messageId) ?? -1
      const previousDiscarded = discardedMessageIds.get(sessionId)
      if (previous && cutoff !== -1) {
        discardedMessageIds.set(
          sessionId,
          new Set([
            ...(previousDiscarded ?? []),
            ...previous.slice(cutoff).map((message) => message.id),
          ]),
        )
        messagesBySession.value[sessionId] = [
          ...previous.slice(0, cutoff),
          {
            ...previous[cutoff]!,
            id: `local-edit-${messageId}`,
            content: prompt.trim(),
            skill_ids: options.skillIds ?? previous[cutoff]!.skill_ids,
          },
        ]
      }
      invalidatePartFetches(sessionId)
      invalidatePartDetails(sessionId)
      try {
        const session = await editHarnessMessage(sessionId, messageId, {
          prompt,
          mode: options.mode,
          model: options.model,
          skill_ids: options.skillIds,
          reasoning_effort: options.reasoningEffort,
        })
        upsertSession(session)
        // A refresh started during admission may still contain the old suffix.
        invalidatePartFetches(sessionId)
        await fetchParts(sessionId)
        recordComposerModelUsage(session, options.model ?? '', options.reasoningEffort ?? '')
      } catch (e: unknown) {
        invalidatePartFetches(sessionId)
        if (previous && cutoff !== -1) messagesBySession.value[sessionId] = previous
        if (previousDiscarded) discardedMessageIds.set(sessionId, previousDiscarded)
        else discardedMessageIds.delete(sessionId)
        if (e instanceof ApiRequestError && e.status === 409) {
          notifications.error('Agent is running — stop it before editing this message.', e.message)
          return
        }
        notifications.error('Edit failed', e instanceof Error ? e.message : 'Unknown error')
      }
    })
  }

  function upsertSession(session: HarnessSession): void {
    const idx = sessions.value.findIndex((s) => s.id === session.id)
    if (idx === -1) {
      sessions.value.unshift(session)
    } else {
      sessions.value[idx] = session
    }
  }

  function isDiscardedMessage(sessionId: string, messageId?: string): boolean {
    return Boolean(messageId && discardedMessageIds.get(sessionId)?.has(messageId))
  }

  // --- Real-time reducers (called from socket handlers) ---

  /** Route by persisted message ID, ignoring discarded or completed turns. */
  function resolveLiveMessage(
    sessionId: string,
    messageId: string,
  ): { messages: HarnessMessage[]; message: HarnessMessage } | null {
    if (isDiscardedMessage(sessionId, messageId)) return null
    const messages = messagesFor(sessionId)
    const existing = messages.find((m) => m.id === messageId)
    if (existing) {
      if (existing.role !== 'assistant') return null
      if (existing.completed_at != null) return null
      return { messages, message: existing }
    }
    // An unknown server id is a fresh turn (e.g. busy arrived late after a
    // missed idle event). Only a known completed id is provably stale.
    return { messages, message: routeAssistantMessage(messages, sessionId, messageId) }
  }

  function handlePartUpdated(
    sessionId: string,
    delta: HarnessPartDelta,
    opts: { step?: number; partId?: string | null; partPosition?: number; messageId: string },
  ): void {
    const routed = resolveLiveMessage(sessionId, opts.messageId)
    if (!routed) return
    applyPartDelta(routed.messages, sessionId, delta, {
      step: opts.step,
      partId: opts.partId,
      partPosition: opts.partPosition,
      messageId: opts.messageId,
    })
    stampRunModel(sessionId, opts.messageId)
  }

  function handleTodoUpdated(sessionId: string, todos: HarnessTodo[]): void {
    todosBySession.value[sessionId] = applyTodoUpdate(todosBySession.value[sessionId] ?? [], {
      todos,
    })
    trimInactiveTranscripts()
  }

  function handleSessionSummary(sessionId: string, summary: string): void {
    const session = sessions.value.find((s) => s.id === sessionId)
    // A newer run already started: its own summary will follow when it settles.
    if (session && session.status === 'idle') session.summary = summary
  }

  /** `harness.steer_queue`: full snapshot of the queued steers. */
  function handleSteerQueue(sessionId: string, steers: HarnessSteer[] | undefined): void {
    setQueuedSteers(sessionId, steers ?? [])
  }

  function handleSubtaskStarted(
    sessionId: string,
    event: {
      subtask_id: string
      agent: string
      description: string
      part_id: string
      part_position?: number
      child_session_id?: string
      model?: string
      reasoning_effort?: string
      message_id: string
    },
  ): void {
    const routed = resolveLiveMessage(sessionId, event.message_id)
    if (!routed) return
    applySubtaskStarted(routed.message, sessionId, {
      workspace_id: '',
      session_id: sessionId,
      message_id: event.message_id,
      subtask_id: event.subtask_id,
      agent: event.agent,
      description: event.description,
      part_id: event.part_id,
      part_position: event.part_position,
      child_session_id: event.child_session_id,
      model: event.model,
      reasoning_effort: event.reasoning_effort,
    })
    routed.message.parts = sortHarnessParts(routed.message.parts)
    stampRunModel(sessionId, event.message_id)
  }

  function handleSubtaskFinished(
    sessionId: string,
    event: {
      subtask_id: string
      agent?: string
      status: string
      summary: string
      child_session_id?: string
      message_id: string
    },
  ): void {
    const routed = resolveLiveMessage(sessionId, event.message_id)
    if (!routed) return
    applySubtaskFinished(routed.message, {
      workspace_id: '',
      session_id: sessionId,
      message_id: event.message_id,
      subtask_id: event.subtask_id,
      agent: event.agent,
      status: event.status,
      summary: event.summary,
      child_session_id: event.child_session_id,
    })
  }

  function stampRunModel(
    sessionId: string,
    messageId?: string,
    extras?: { model?: string; reasoning_effort?: string },
  ): void {
    const session = sessions.value.find((s) => s.id === sessionId)
    const messages = messagesBySession.value[sessionId]
    const target = messageId
      ? messages?.find((m) => m.id === messageId && m.role === 'assistant')
      : messages?.[messages.length - 1]
    const last = target?.role === 'assistant' ? target : undefined
    if (!last || last.completed_at != null) return
    const model = extras?.model || session?.model || ''
    const effort =
      extras?.reasoning_effort !== undefined
        ? extras.reasoning_effort
        : (session?.reasoning_effort ?? '')
    if (!last.model && model) last.model = model
    if (!last.reasoning_effort && effort) last.reasoning_effort = effort
  }

  function stampAssistantCompleted(sessionId: string, messageId?: string): void {
    const messages = messagesBySession.value[sessionId]
    const target = messageId
      ? messages?.find((m) => m.id === messageId && m.role === 'assistant')
      : messages?.[messages.length - 1]
    const last = target?.role === 'assistant' ? target : undefined
    if (!last || last.completed_at != null) return
    last.completed_at = new Date().toISOString()
  }

  function handleSessionStatus(
    sessionId: string,
    status: HarnessSession['status'],
    extras?: {
      model?: string
      reasoning_effort?: string
      message_id?: string
      user_message_id?: string
    },
  ): void {
    if (isDiscardedMessage(sessionId, extras?.message_id)) return
    const session = sessions.value.find((s) => s.id === sessionId)
    if (session) {
      session.status = status
      // The summary describes the finished run; the backend clears it too.
      if (status === 'busy') session.summary = ''
      // Busy sessions remain protected; account for streamed growth once the
      // run settles instead of recalculating its size on every delta.
      if (extras?.model) session.model = extras.model
      if (extras?.reasoning_effort !== undefined) {
        session.reasoning_effort = extras.reasoning_effort
      }
    }
    if (status === 'busy') {
      // Already-completed parts are immutable history and remain detail-loadable
      // while the new turn runs; never invalidate their in-flight hydration.
      // Anchor the fresh turn before the first delta arrives: create the
      // server-id assistant shell after the (possibly optimistic)
      // follow-up user so deltas route to the new turn — never to the
      // previous answer — and Thinking shows on the fresh empty turn.
      if (extras?.message_id) {
        ensureBusyAssistant(
          messagesFor(sessionId),
          sessionId,
          extras.message_id,
          extras.user_message_id,
        )
      }
      stampRunModel(sessionId, extras?.message_id, extras)
      if (session && !session.manual_unread) session.unread = false
      return
    }
    stampRunModel(sessionId, extras?.message_id, extras)
    if (status === 'idle') {
      stampAssistantCompleted(sessionId, extras?.message_id)
      // Include sessions first populated only by streamed deltas in the LRU.
      touchTranscript(sessionId, true)
      trimInactiveTranscripts()
      if (viewingSessionId.value === sessionId && !session?.manual_unread) {
        if (session) session.unread = true
        void markSessionRead(sessionId)
        return
      }
      if (session) session.unread = true
      return
    }
    if (session && !session.manual_unread) session.unread = false
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

  function handleQuestionRequired(request: HarnessQuestionRequest): void {
    pendingQuestions.value[request.request_id] = { ...request, status: 'pending' }
  }

  function handleQuestionResolved(requestId: string, status: string): void {
    const request = pendingQuestions.value[requestId]
    if (request) {
      const auth = useAuthStore()
      clearQuestionInputCache(
        { userId: auth.user?.id, organizationId: auth.activeOrganizationId },
        request,
      )
    }
    delete pendingQuestions.value[requestId]
    if (status === 'answered') return
  }

  async function resolveQuestion(
    sessionId: string,
    requestId: string,
    answers: string[],
    reject = false,
  ): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    const request = pendingQuestions.value[requestId]
    if (!request) return
    const auth = useAuthStore()
    const identity = { userId: auth.user?.id, organizationId: auth.activeOrganizationId }
    try {
      await resolveHarnessQuestion(sessionId, requestId, answers, reject)
      // Navigation may reset pending state or switch accounts while the POST is in flight.
      clearQuestionInputCache(identity, request)
      if (
        auth.user?.id === identity.userId &&
        auth.activeOrganizationId === identity.organizationId &&
        pendingQuestions.value[requestId]?.session_id === request.session_id &&
        pendingQuestions.value[requestId]?.workspace_id === request.workspace_id
      )
        delete pendingQuestions.value[requestId]
    } catch (e: unknown) {
      notifications.error('Question failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function resolvePermission(
    sessionId: string,
    requestId: string,
    response: HarnessPermissionResponse,
  ): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    const request = pendingPermissions.value[requestId]
    if (!request) return
    try {
      const outcome = await resolveHarnessPermission(sessionId, request, response)
      handlePermissionResolved(requestId, outcome.decision)
    } catch (e: unknown) {
      notifications.error('Permission failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function dismissNotice(messageId: string): Promise<void> {
    const sessionId = activeSessionId.value
    dismissedNoticeIds.value = { ...dismissedNoticeIds.value, [messageId]: true }
    const message = sessionId
      ? (messagesBySession.value[sessionId] ?? []).find((item) => item.id === messageId)
      : undefined
    if (message) message.notice_dismissed_at = new Date().toISOString()
    if (!sessionId) return
    // Local `local-user-*` ids never exist on the server; the local cache covers them.
    if (messageId.startsWith('local-')) return
    try {
      await dismissHarnessNotice(sessionId, messageId)
    } catch (e: unknown) {
      const next = { ...dismissedNoticeIds.value }
      delete next[messageId]
      dismissedNoticeIds.value = next
      if (message) message.notice_dismissed_at = null
      const notifications = useNotificationStore()
      notifications.error('Dismiss failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  // Load agent configs (cached); failures yield [].
  async function loadAgentConfigs(): Promise<void> {
    try {
      agentConfigs.value = await loadAgentConfigsCached()
    } catch {
      agentConfigs.value = []
    }
  }

  // Agent default {model, effort} for a mode.
  function agentDefault(mode: HarnessSessionMode): { model: string; effort: string } {
    const found =
      agentConfigs.value.find((item) => item.agent === mode && item.mode === 'primary') ??
      agentConfigs.value.find((item) => item.agent === mode)
    return { model: found?.model ?? '', effort: found?.effort ?? '' }
  }

  // Apply mode defaults unless the user customized the composer.
  function ensureComposerDefaults(mode: HarnessSessionMode, catalog: ProviderModel[] = []): void {
    if (composerDirty.value) return
    const defaults = agentDefault(mode)
    modelInput.value = defaults.model
    const catalogModel = resolveCatalogModel(catalog, defaults.model)
    effortInput.value = catalogModel ? snapEffort(catalogModel, defaults.effort) : defaults.effort
  }

  // Manual model change.
  function setComposerModel(v: string): void {
    modelInput.value = v
    composerDirty.value = true
  }

  // Manual effort change.
  function setComposerEffort(v: string): void {
    effortInput.value = v
    composerDirty.value = true
  }

  // Mark dirty without changing values (child-driven updates).
  function markComposerDirty(): void {
    composerDirty.value = true
  }

  // Load session values into the composer; resets dirty.
  function loadSessionIntoComposer(model: string, effort: string): void {
    modelInput.value = model
    effortInput.value = effort
    composerDirty.value = false
  }

  // Force-apply mode defaults (clears dirty first).
  function resetComposer(mode: HarnessSessionMode, catalog: ProviderModel[] = []): void {
    composerDirty.value = false
    ensureComposerDefaults(mode, catalog)
  }

  function reset(): void {
    clearTranscriptCaches()
    sessions.value = []
    activeSelectionGeneration += 1
    sessionsGeneration += 1
    activeSessionsWorkspace = null
    sessionFetches.clear()
    setActiveSession(null)
    viewingSessionId.value = null
    loading.value = false
    sessionsLoaded.value = false
    sessionsLoadedWorkspace.value = null
    error.value = null
    agentConfigs.value = []
    composerDirty.value = false
  }

  return {
    // State
    sessions,
    messagesBySession,
    todosBySession,
    pendingPermissions,
    pendingQuestions,
    queuedSteers,
    stoppingSessionIds,
    activeSessionId,
    viewingSessionId,
    loading,
    sessionsLoaded,
    sessionsLoadedWorkspace,
    error,
    modelInput,
    effortInput,
    agentConfigs,
    composerDirty,
    dismissedNoticeIds,
    // Getters
    activeSession,
    activeMessages,
    activeTodos,
    activeQueuedSteers,
    activePermissionRequests,
    activeQuestionRequests,
    activeSessionLineage,
    // Actions
    fetchSessions,
    setActiveSession,
    setViewingSession,
    markSessionRead,
    fetchParts,
    fetchPartDetail,
    pinPartDetail,
    detailErrors,
    refreshPartsAfterIdle,
    fetchTodos,
    createSession,
    sendMessage,
    steerSession,
    withdrawSteer,
    ensureUserTurnLoaded,
    forkSession,
    editMessage,
    renameSession,
    removeSession,
    updateSessionMode,
    abortSession,
    loadAgentConfigs,
    agentDefault,
    ensureComposerDefaults,
    setComposerModel,
    setComposerEffort,
    markComposerDirty,
    loadSessionIntoComposer,
    resetComposer,
    dismissNotice,
    resolvePermission,
    resolveQuestion,
    // Real-time
    messagesFor,
    handlePartUpdated,
    handleTodoUpdated,
    handleSessionSummary,
    handleSteerQueue,
    handleSubtaskStarted,
    handleSubtaskFinished,
    handleSessionStatus,
    handlePermissionRequired,
    handlePermissionResolved,
    handleQuestionRequired,
    handleQuestionResolved,
    reset,
  }
})
