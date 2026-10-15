/**
 * Harness Pinia store.
 *
 * Owns the harness block model (`HarnessSession` + `HarnessMessage` with
 * `parts[]` + todos + pending permission requests). All streaming
 * transitions delegate to the pure reducers in `lib/harnessReducer.ts`.
 */

import { defineStore } from 'pinia'
import { computed, ref, toRaw } from 'vue'

import type {
  HarnessMessage,
  HarnessPart,
  HarnessPartDelta,
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSessionMode,
  HarnessId,
  HarnessTodo,
} from '@/types/harness'
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
} from '@/services/harness.api'
import { ApiRequestError } from '@/services/api'
import { hydrateHarnessPart } from '@/lib/toolDisplay'
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureAssistantMessage,
  ensureBusyAssistant,
  mergeBusyFetchedMessages,
  routeAssistantMessage,
  settleOpenStreamParts,
  sortHarnessMessages,
  sortHarnessParts,
} from '@/lib/harnessReducer'
import {
  collectAncestorSessions,
  collectDescendantSessionIds,
  collectRunningChildSessionIds,
} from '@/lib/harnessSubtaskActivity'
import { loadAgentConfigsCached } from '@/lib/agentConfigs'
import { recordRecentModelUsage } from '@/lib/recentModels'
import type { AgentConfig } from '@/lib/harnessAgents'
import { resolveCatalogModel, snapEffort, type ProviderModel } from '@/lib/harnessModels'
import { useWorkspaceStore } from './workspaces'
import { useNotificationStore } from './notifications'
import { useHarnessConversationStore } from './harnessConversations'

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
  const partFetches = new Map<string, Promise<void>>()
  const todoFetches = new Map<string, Promise<void>>()
  const partGenerations = new Map<string, number>()
  const detailGenerations = new Map<string, number>()
  const detailFetches = new Map<string, Promise<boolean>>()
  const detailCache = new Map<string, HarnessPart>()
  const pinnedDetailKeys = new Set<string>()
  const detailErrors = ref<Record<string, string>>({})
  const DETAIL_CACHE_LIMIT = 100
  // Inactive hydrated transcripts are a UI cache only; the server remains
  // authoritative. Protected sessions may exceed either inactive-cache limit.
  const INACTIVE_TRANSCRIPT_LIMIT = 4
  const INACTIVE_TRANSCRIPT_BUDGET_BYTES = 16 * 1024 * 1024
  /** Session -> estimated payload bytes, ordered least- to most-recently used. */
  const transcriptLru = new Map<string, number>()
  let activeSelectionGeneration = 0
  const todoGenerations = new Map<string, number>()
  let sessionsGeneration = 0
  let activeSessionsWorkspace: string | null = null
  const readFlights = new Map<string, Promise<boolean>>()

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

  const rootSessions = computed(() => sessions.value.filter((session) => !session.parent_id))

  const childSessionsByParent = computed(() => {
    const map: Record<string, HarnessSession[]> = {}
    for (const session of sessions.value) {
      if (!session.parent_id) continue
      if (!map[session.parent_id]) map[session.parent_id] = []
      map[session.parent_id]!.push(session)
    }
    for (const children of Object.values(map)) {
      children.sort((a, b) => {
        const aTime = a.created_at ? new Date(a.created_at).getTime() : 0
        const bTime = b.created_at ? new Date(b.created_at).getTime() : 0
        return aTime - bTime
      })
    }
    return map
  })

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

  /** Estimate retained transcript size without allocating a serialized copy. */
  function estimateTranscriptBytes(messages: HarnessMessage[]): number {
    const stack: Array<{ value: unknown; multiplier: number }> = [
      { value: messages, multiplier: 1 },
    ]
    const visited = new WeakMap<object, number>()
    let bytes = 0

    while (stack.length) {
      const { value, multiplier } = stack.pop()!
      if (typeof value === 'string') {
        bytes += value.length * 2 * multiplier
      } else if (
        typeof value === 'number' ||
        typeof value === 'boolean' ||
        typeof value === 'bigint'
      ) {
        bytes += 8 * multiplier
      } else if (value && typeof value === 'object') {
        const raw = toRaw(value)
        const previousMultiplier = visited.get(raw) ?? 0
        if (previousMultiplier >= multiplier) continue

        const isDetailedPart =
          !Array.isArray(raw) &&
          'detail_loaded' in raw &&
          raw.detail_loaded === true &&
          'type' in raw &&
          ['tool', 'reasoning', 'agent', 'patch', 'compaction'].includes(String(raw.type))
        const effectiveMultiplier = isDetailedPart ? multiplier * 2 : multiplier
        const additionalMultiplier = effectiveMultiplier - previousMultiplier
        visited.set(raw, effectiveMultiplier)

        if (Array.isArray(raw)) {
          bytes += (24 + raw.length * 8) * additionalMultiplier
          for (const item of raw) stack.push({ value: item, multiplier: effectiveMultiplier })
        } else {
          bytes += 32 * additionalMultiplier
          for (const key of Object.keys(raw)) {
            bytes += (key.length * 2 + 8) * additionalMultiplier
            stack.push({
              value: (raw as Record<string, unknown>)[key],
              multiplier: effectiveMultiplier,
            })
          }
        }
      }
    }

    return bytes
  }

  function touchTranscript(sessionId: string, recalculateBytes = false): void {
    const messages = messagesBySession.value[sessionId]
    if (!messages) return
    const bytes =
      recalculateBytes || !transcriptLru.has(sessionId)
        ? estimateTranscriptBytes(messages)
        : transcriptLru.get(sessionId)!
    transcriptLru.delete(sessionId)
    transcriptLru.set(sessionId, bytes)
  }

  /** Protect a lineage family when its contents support the active subtask UI. */
  function protectedTranscriptSessions(): Set<string> {
    const protectedIds = new Set<string>()
    const protectFamily = (sessionId: string): void => {
      protectedIds.add(sessionId)
      for (const ancestor of collectAncestorSessions(sessionId, sessions.value)) {
        protectedIds.add(ancestor.id)
      }
      for (const descendantId of collectDescendantSessionIds(sessionId, sessions.value)) {
        protectedIds.add(descendantId)
      }
    }

    for (const sessionId of [activeSessionId.value, viewingSessionId.value]) {
      if (sessionId) protectFamily(sessionId)
    }
    for (const session of sessions.value) {
      if (session.status === 'busy') protectFamily(session.id)
    }
    for (const request of Object.values(pendingPermissions.value)) {
      protectFamily(request.session_id)
    }
    for (const request of Object.values(pendingQuestions.value)) {
      protectFamily(request.session_id)
    }
    // A live fetch or explicitly pinned detail is an explicit temporary cache pin.
    for (const key of pinnedDetailKeys) protectFamily(key.slice(0, key.lastIndexOf(':')))
    const inFlightSessionIds = new Set([
      ...[...partFetches.keys()].map((key) => key.slice(0, key.lastIndexOf(':'))),
      ...[...detailFetches.keys()].map((key) => key.slice(0, key.lastIndexOf(':'))),
      ...todoFetches.keys(),
    ])
    for (const sessionId of inFlightSessionIds) protectFamily(sessionId)
    return protectedIds
  }

  function forgetTranscript(sessionId: string): void {
    transcriptLru.delete(sessionId)
    partGenerations.set(sessionId, (partGenerations.get(sessionId) ?? 0) + 1)
    detailGenerations.set(sessionId, (detailGenerations.get(sessionId) ?? 0) + 1)
    todoGenerations.set(sessionId, (todoGenerations.get(sessionId) ?? 0) + 1)
    for (const key of partFetches.keys()) {
      if (key.startsWith(`${sessionId}:`)) partFetches.delete(key)
    }
    for (const key of detailFetches.keys()) {
      if (key.startsWith(`${sessionId}:`)) detailFetches.delete(key)
    }
    todoFetches.delete(sessionId)
    for (const [key, part] of detailCache) {
      if (part.session_id !== sessionId) continue
      detailCache.delete(key)
      pinnedDetailKeys.delete(key)
    }
    for (const key of pinnedDetailKeys) {
      if (key.startsWith(`${sessionId}:`)) pinnedDetailKeys.delete(key)
    }
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
  }

  /** Keep only a small LRU of unprotected inactive transcripts. */
  function trimInactiveTranscripts(): void {
    const protectedIds = protectedTranscriptSessions()
    let inactiveCount = 0
    let inactiveBytes = 0
    for (const [sessionId, bytes] of transcriptLru) {
      if (protectedIds.has(sessionId)) continue
      inactiveCount += 1
      inactiveBytes += bytes
    }
    for (const sessionId of transcriptLru.keys()) {
      if (
        inactiveCount <= INACTIVE_TRANSCRIPT_LIMIT &&
        inactiveBytes <= INACTIVE_TRANSCRIPT_BUDGET_BYTES
      ) {
        break
      }
      if (protectedIds.has(sessionId)) continue
      const bytes = transcriptLru.get(sessionId) ?? 0
      forgetTranscript(sessionId)
      inactiveCount -= 1
      inactiveBytes -= bytes
    }
  }

  /** Clear transcript caches and invalidate all outstanding session fetches. */
  function clearTranscriptCaches(): void {
    const sessionIdFromKey = (key: string): string => key.slice(0, key.indexOf(':'))
    const partSessionIds = new Set([
      ...partGenerations.keys(),
      ...[...partFetches.keys()].map(sessionIdFromKey),
    ])
    const detailSessionIds = new Set([
      ...detailGenerations.keys(),
      ...[...detailFetches.keys()].map(sessionIdFromKey),
    ])
    const todoSessionIds = new Set([...todoGenerations.keys(), ...todoFetches.keys()])
    for (const sessionId of partSessionIds) {
      partGenerations.set(sessionId, (partGenerations.get(sessionId) ?? 0) + 1)
    }
    for (const sessionId of detailSessionIds) {
      detailGenerations.set(sessionId, (detailGenerations.get(sessionId) ?? 0) + 1)
    }
    for (const sessionId of todoSessionIds) {
      todoGenerations.set(sessionId, (todoGenerations.get(sessionId) ?? 0) + 1)
    }
    transcriptLru.clear()
    partFetches.clear()
    detailFetches.clear()
    todoFetches.clear()
    detailCache.clear()
    pinnedDetailKeys.clear()
    detailErrors.value = {}
    messagesBySession.value = {}
    todosBySession.value = {}
    pendingPermissions.value = {}
    pendingQuestions.value = {}
    dismissedNoticeIds.value = {}
    readFlights.clear()
  }

  // --- Actions ---

  function rememberPartDetail(sessionId: string, part: HarnessPart): void {
    if (
      !part.detail_loaded ||
      !['tool', 'reasoning', 'agent', 'patch', 'compaction'].includes(part.type)
    )
      return
    const key = `${sessionId}:${part.id}`
    detailCache.delete(key)
    detailCache.set(key, {
      ...part,
      input: part.input ? { ...part.input } : {},
      meta: part.meta ? { ...part.meta } : {},
    })
    while (detailCache.size > DETAIL_CACHE_LIMIT) {
      const oldest = [...detailCache.keys()].find((candidate) => !pinnedDetailKeys.has(candidate))
      if (oldest === undefined) break
      const evicted = detailCache.get(oldest)
      detailCache.delete(oldest)
      if (!evicted) continue
      const stored = messagesBySession.value[evicted.session_id]
        ?.flatMap((message) => message.parts)
        .find((item) => item.id === evicted.id)
      if (stored?.detail_loaded && !pinnedDetailKeys.has(`${evicted.session_id}:${evicted.id}`)) {
        stored.input = stored.type === 'tool' ? { tool: stored.tool ?? '' } : {}
        if (!(stored.state === 'error' && stored.output === 'Tool failed')) stored.output = ''
        stored.meta = stored.display?.agent_meta ? { agent_meta: stored.display.agent_meta } : {}
        stored.detail_loaded = false
      }
    }
  }

  function pinPartDetail(sessionId: string, partId: string, pinned: boolean): void {
    const key = `${sessionId}:${partId}`
    if (pinned) {
      pinnedDetailKeys.add(key)
      const cached = detailCache.get(key)
      if (cached) {
        detailCache.delete(key)
        detailCache.set(key, cached)
      }
    } else {
      pinnedDetailKeys.delete(key)
      trimInactiveTranscripts()
    }
  }

  function invalidatePartDetails(sessionId: string, clear = false): void {
    detailGenerations.set(sessionId, (detailGenerations.get(sessionId) ?? 0) + 1)
    if (!clear) return
    for (const [key, cached] of detailCache) {
      if (cached.session_id !== sessionId) continue
      detailCache.delete(key)
      pinnedDetailKeys.delete(key)
      const stored = messagesBySession.value[sessionId]
        ?.flatMap((message) => message.parts)
        .find((item) => item.id === cached.id)
      if (stored) {
        stored.input = stored.type === 'tool' ? { tool: stored.tool ?? '' } : {}
        // Keep bounded live previews and generic error status text.
        if (!(stored.state === 'error' && stored.output === 'Tool failed')) stored.output = ''
        const lightweightMeta: Record<string, unknown> = {}
        if (typeof stored.meta?.['step'] === 'number') lightweightMeta.step = stored.meta['step']
        if (stored.display?.agent_meta) lightweightMeta.agent_meta = stored.display.agent_meta
        stored.meta = lightweightMeta
        stored.detail_loaded = false
      }
    }
  }

  async function fetchPartDetail(sessionId: string, partId: string): Promise<boolean> {
    const message = messagesBySession.value[sessionId]?.find((item) =>
      item.parts.some((part) => part.id === partId),
    )
    const part = message?.parts.find((item) => item.id === partId)
    if (!part || part.detail_loaded) return Boolean(part?.detail_loaded)
    if (part.state === 'pending' || part.state === 'running') return false
    const key = `${sessionId}:${partId}`
    const existing = detailFetches.get(key)
    if (existing) return existing
    const sessionGeneration = detailGenerations.get(sessionId) ?? 0
    const selectionGeneration = activeSelectionGeneration
    const partGeneration = partGenerations.get(sessionId) ?? 0
    const flight = (async () => {
      try {
        // A finished part can be expanded while its containing conversation
        // is still busy. Never fetch a pending/running part, but do not gate
        // historical/completed rows on the session's overall busy status.
        if (part.state === 'pending' || part.state === 'running') return false
        const detail = await getHarnessPart(sessionId, partId)
        const stillExists = sessions.value.some((item) => item.id === sessionId)
        if (
          !stillExists ||
          activeSessionId.value !== sessionId ||
          selectionGeneration !== activeSelectionGeneration ||
          sessionGeneration !== (detailGenerations.get(sessionId) ?? 0) ||
          partGeneration !== (partGenerations.get(sessionId) ?? 0)
        )
          return false
        const currentMessage = messagesBySession.value[sessionId]?.find(
          (item) => item.id === message!.id,
        )
        const currentPart = currentMessage?.parts.find((item) => item.id === partId)
        if (!currentPart || currentPart.state === 'pending' || currentPart.state === 'running')
          return false
        // A run transition racing the request invalidates only unfinished parts;
        // completed/error rows remain safe to hydrate during an active run.
        Object.assign(currentPart, detail, {
          session_id: sessionId,
          message_id: currentMessage!.id,
          detail_loaded: true,
        })
        rememberPartDetail(sessionId, currentPart)
        touchTranscript(sessionId, true)
        const errors = { ...detailErrors.value }
        delete errors[key]
        detailErrors.value = errors
        return true
      } catch (e: unknown) {
        if (
          sessionGeneration === (detailGenerations.get(sessionId) ?? 0) &&
          selectionGeneration === activeSelectionGeneration &&
          partGeneration === (partGenerations.get(sessionId) ?? 0)
        ) {
          detailErrors.value = {
            ...detailErrors.value,
            [key]: e instanceof Error ? e.message : 'Failed to load part details',
          }
        }
        throw e
      }
    })().finally(() => {
      if (detailFetches.get(key) === flight) {
        detailFetches.delete(key)
        trimInactiveTranscripts()
      }
    })
    detailFetches.set(key, flight)
    return flight
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

  async function fetchParts(sessionId: string, hydrateChildren = true): Promise<void> {
    const key = `${sessionId}:${hydrateChildren}`
    let flight = partFetches.get(key)
    if (!flight) {
      const generation = (partGenerations.get(sessionId) ?? 0) + 1
      partGenerations.set(sessionId, generation)
      flight = (async () => {
        try {
          const response = await listHarnessParts(sessionId)
          if (partGenerations.get(sessionId) !== generation) return
          const previous = messagesBySession.value[sessionId] ?? []
          const previousByPart = new Map(
            previous.flatMap((row) => row.parts.map((part) => [part.id, part] as const)),
          )
          const incoming = sortHarnessMessages(
            response.messages.map((message) => {
              const parts = (message.parts ?? []).map((part) => {
                const prior = previousByPart.get(part.id)
                const cached = detailCache.get(`${sessionId}:${part.id}`)
                const source = prior?.detail_loaded ? prior : cached
                const mergedPart = source
                  ? {
                      ...part,
                      input: source.input,
                      output: source.output,
                      meta: source.meta,
                      detail_loaded: true,
                    }
                  : part
                const hydratedPart = hydrateHarnessPart({
                  ...mergedPart,
                  session_id: sessionId,
                  message_id: message.id,
                })
                if (hydratedPart.detail_loaded) rememberPartDetail(sessionId, hydratedPart)
                return hydratedPart
              })
              const content =
                message.role === 'assistant' && !message.content
                  ? parts
                      .filter((part) => part.type === 'text')
                      .map((part) => part.output)
                      .join('')
                  : message.content
              return { ...message, content, session_id: sessionId, parts }
            }),
          )
          const session = sessions.value.find((item) => item.id === sessionId)
          messagesBySession.value[sessionId] =
            session?.status === 'busy'
              ? mergeBusyFetchedMessages(previous, incoming)
              : settleOpenStreamParts(incoming)
          touchTranscript(sessionId, true)
          // Timeline parts intentionally omit parent ids. Restore them from
          // the enclosing REST envelope after reconciliation too, since a
          // busy merge can carry forward live-only rows from the previous
          // message snapshot.
          for (const message of messagesBySession.value[sessionId] ?? []) {
            message.session_id = sessionId
            for (const part of message.parts) {
              part.session_id = sessionId
              part.message_id = message.id
            }
          }
          const nextPermissions = { ...pendingPermissions.value }
          for (const id of Object.keys(nextPermissions)) {
            if (nextPermissions[id]?.session_id === sessionId) delete nextPermissions[id]
          }
          for (const request of response.permissions ?? []) {
            nextPermissions[request.request_id] = { ...request, status: 'pending' }
          }
          pendingPermissions.value = nextPermissions
          const nextQuestions = { ...pendingQuestions.value }
          for (const id of Object.keys(nextQuestions)) {
            if (nextQuestions[id]?.session_id === sessionId) delete nextQuestions[id]
          }
          for (const request of response.questions ?? []) {
            nextQuestions[request.request_id] = { ...request, status: 'pending' }
          }
          pendingQuestions.value = nextQuestions
          if (hydrateChildren) {
            const childIds = collectRunningChildSessionIds(messagesBySession.value[sessionId] ?? [])
            await Promise.all(childIds.map((childId) => fetchParts(childId, false)))
          }
        } catch (e: unknown) {
          if (partGenerations.get(sessionId) === generation) {
            useNotificationStore().error(
              'Failed to load messages',
              e instanceof Error ? e.message : 'Unknown error',
            )
          }
        }
      })().finally(() => {
        if (partFetches.get(key) === flight) {
          partFetches.delete(key)
          trimInactiveTranscripts()
        }
      })
      partFetches.set(key, flight)
    }
    await flight
  }

  /**
   * The idle event needs a snapshot taken *after* the run completed. If an
   * earlier busy fetch is still in flight, awaiting the deduplicated request
   * would only replay its stale snapshot and leave the final answer missing.
   */
  async function refreshPartsAfterIdle(sessionId: string): Promise<void> {
    const ongoing = [...partFetches.entries()]
      .filter(([key]) => key.startsWith(`${sessionId}:`))
      .map(([, flight]) => flight)
    if (ongoing.length) await Promise.all(ongoing)
    await fetchParts(sessionId)
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
      if (harnessId === 'native') recordRecentModelUsage(model, reasoningEffort)
      return session
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  async function sendMessage(
    sessionId: string,
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
      if ((session.harness_id ?? previousSession?.harness_id) !== 'claude')
        recordRecentModelUsage(options.model ?? '', options.reasoningEffort ?? '')
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
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

  async function abortSession(sessionId: string): Promise<void> {
    if (sessionTransitioning(sessionId)) return
    const notifications = useNotificationStore()
    try {
      const session = await abortHarnessSession(sessionId)
      upsertSession(session)
    } catch (e: unknown) {
      notifications.error('Abort failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  /**
   * Fork after an assistant answer, before a user prompt, or the full session.
   * Only user cutoffs return a composer prefill; forking never starts a run.
   */
  async function forkSession(
    sessionId: string,
    messageId?: string,
  ): Promise<{ session: HarnessSession; prefill: string } | null> {
    if (sessionTransitioning(sessionId)) return null
    const notifications = useNotificationStore()
    try {
      const message = messagesBySession.value[sessionId]?.find((item) => item.id === messageId)
      const prefill = message?.role === 'user' ? message.content : ''
      const session = await forkHarnessSession(
        sessionId,
        messageId
          ? {
              message_id: messageId,
              ...(message?.role === 'assistant' ? { include_message: true } : {}),
            }
          : {},
      )
      upsertSession(session)
      setActiveSession(session.id)
      await fetchParts(session.id)
      void useHarnessConversationStore().fetchConversations()
      return { session, prefill }
    } catch (e: unknown) {
      notifications.error('Fork failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  /**
   * Edit a user message and rerun the session from there (OpenCode
   * `session.revert` parity). The panel socket refetches on
   * `session_status:idle`; an optimistic fetch covers the gap.
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
    try {
      invalidatePartDetails(sessionId, true)
      const session = await editHarnessMessage(sessionId, messageId, {
        prompt,
        mode: options.mode,
        model: options.model,
        skill_ids: options.skillIds,
        reasoning_effort: options.reasoningEffort,
      })
      upsertSession(session)
      await fetchParts(sessionId)
    } catch (e: unknown) {
      if (e instanceof ApiRequestError && e.status === 409) {
        notifications.error('Agent is running — stop it before editing this message.', e.message)
        return
      }
      notifications.error('Edit failed', e instanceof Error ? e.message : 'Unknown error')
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

  /**
   * Route a live delta to its per-turn assistant shell.
   *
   * Stale-event guard: a `messageId` that matches a *completed* older turn
   * is dropped (it must never attach to the latest turn); a `messageId`
   * that matches nothing while a fresh empty assistant already streams
   * is also dropped when it looks older (prevents resurrection). The
   * reducer itself owns the same-turn routing via `routeAssistantMessage`.
   */
  function resolveLiveMessage(
    sessionId: string,
    messageId?: string,
  ): { messages: HarnessMessage[]; message: HarnessMessage } | null {
    const messages = messagesFor(sessionId)
    if (!messageId) {
      return { messages, message: ensureAssistantMessage(messages, sessionId) }
    }
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
    opts: { step?: number; partId?: string; partPosition?: number; messageId?: string } = {},
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

  function handleSubtaskStarted(
    sessionId: string,
    event: {
      subtask_id: string
      agent: string
      description: string
      part_id?: string
      part_position?: number
      child_session_id?: string
      model?: string
      reasoning_effort?: string
      message_id?: string
    },
  ): void {
    const routed = resolveLiveMessage(sessionId, event.message_id)
    if (!routed) return
    applySubtaskStarted(routed.message, sessionId, {
      workspace_id: '',
      session_id: sessionId,
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
      message_id?: string
    },
  ): void {
    const routed = resolveLiveMessage(sessionId, event.message_id)
    if (!routed) return
    applySubtaskFinished(routed.message, {
      workspace_id: '',
      session_id: sessionId,
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
    const session = sessions.value.find((s) => s.id === sessionId)
    if (session) {
      session.status = status
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
    if (!pendingQuestions.value[requestId]) return
    try {
      const outcome = await resolveHarnessQuestion(sessionId, requestId, answers, reject)
      handleQuestionResolved(requestId, outcome.status)
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

  // Clear the dirty flag.
  function resetComposerDirty(): void {
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
    activePermissionRequests,
    activeQuestionRequests,
    rootSessions,
    childSessionsByParent,
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
    resetComposerDirty,
    resetComposer,
    dismissNotice,
    resolvePermission,
    resolveQuestion,
    // Real-time
    messagesFor,
    handlePartUpdated,
    handleTodoUpdated,
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
