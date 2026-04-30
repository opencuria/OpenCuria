/**
 * Workspace Pinia store.
 *
 * Manages workspace state, sessions, and real-time output streaming.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import type {
  Workspace,
  WorkspaceDetail,
  WorkspaceCreateIn,
  WorkspaceUpdateIn,
  Session,
  SessionSkill,
  Skill,
  Chat,
  ImageArtifact,
  ImageArtifactCreateIn,
  ImageArtifactCloneIn,
} from '@/types'
import { WorkspaceOperation, WorkspaceStatus, SessionStatus } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { isSessionActive } from '@/lib/sessionState'
import { useNotificationStore } from './notifications'
import { useImageStore } from './images'
import { useSkillStore } from './skills'

/** Sentinel ID used for chats that have not yet been persisted to the backend. */
export const PENDING_CHAT_ID = '__pending__'

type PendingWorkspaceOperationType = 'create' | 'start' | 'stop' | 'remove'

interface PendingWorkspaceOperation {
  operation: PendingWorkspaceOperationType
  expectedStatus: WorkspaceStatus
}

export const useWorkspaceStore = defineStore('workspaces', () => {
  // --- State ---
  const workspaces = ref<Workspace[]>([])
  const activeWorkspace = ref<WorkspaceDetail | null>(null)
  const activeSessions = ref<Session[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const chats = ref<Chat[]>([])
  const activeChatId = ref<string | null>(null)
  const supportsMultiChat = ref(false)
  const imageArtifacts = ref<ImageArtifact[]>([])
  const pendingWorkspaceOperations = ref<Record<string, PendingWorkspaceOperation>>({})

  // --- Getters ---
  const runningWorkspaces = computed(() =>
    workspaces.value.filter((w) => w.status === WorkspaceStatus.RUNNING),
  )

  const workspacesByStatus = computed(() => ({
    creating: workspaces.value.filter((w) => w.status === WorkspaceStatus.CREATING),
    running: workspaces.value.filter((w) => w.status === WorkspaceStatus.RUNNING),
    stopped: workspaces.value.filter((w) => w.status === WorkspaceStatus.STOPPED),
    failed: workspaces.value.filter((w) => w.status === WorkspaceStatus.FAILED),
    pending_deletion: workspaces.value.filter((w) => w.status === WorkspaceStatus.PENDING_DELETION),
    deleting: workspaces.value.filter((w) => w.status === WorkspaceStatus.DELETING),
    deleted: workspaces.value.filter((w) => w.status === WorkspaceStatus.DELETED),
    delete_failed: workspaces.value.filter((w) => w.status === WorkspaceStatus.DELETE_FAILED),
  }))

  /** Sessions filtered to the active chat. Returns empty array if no active chat is selected. */
  const activeChatSessions = computed<Session[]>(() => {
    if (!activeChatId.value) return []
    return activeSessions.value
  })

  /** The currently selected chat object. */
  const activeChat = computed(() =>
    chats.value.find((c) => c.id === activeChatId.value) ?? null,
  )

  // --- Actions ---

  function setPendingWorkspaceOperation(
    workspaceId: string,
    operation: PendingWorkspaceOperationType,
    expectedStatus: WorkspaceStatus,
  ): void {
    pendingWorkspaceOperations.value[workspaceId] = { operation, expectedStatus }
  }

  function clearPendingWorkspaceOperation(workspaceId: string): void {
    if (pendingWorkspaceOperations.value[workspaceId]) {
      delete pendingWorkspaceOperations.value[workspaceId]
    }
  }

  function getWorkspaceName(workspaceId: string): string {
    const ws =
      workspaces.value.find((workspace) => workspace.id === workspaceId) ??
      (activeWorkspace.value?.id === workspaceId ? activeWorkspace.value : null)
    return ws?.name || `Workspace ${workspaceId.slice(0, 8)}`
  }

  function isWorkspaceTransitioning(workspaceId: string): boolean {
    const workspace =
      workspaces.value.find((item) => item.id === workspaceId) ??
      (activeWorkspace.value?.id === workspaceId ? activeWorkspace.value : null)
    if (!workspace) return Boolean(pendingWorkspaceOperations.value[workspaceId])
    // Workspaces in deletion states are always "transitioning" (no actions allowed)
    const deletionStates: string[] = [
      WorkspaceStatus.PENDING_DELETION,
      WorkspaceStatus.DELETING,
      WorkspaceStatus.DELETED,
    ]
    if (deletionStates.includes(workspace.status)) return true
    return Boolean(workspace.active_operation || pendingWorkspaceOperations.value[workspaceId])
  }

  function getWorkspaceTransitionLabel(workspaceId: string): string | null {
    const workspace =
      workspaces.value.find((item) => item.id === workspaceId) ??
      (activeWorkspace.value?.id === workspaceId ? activeWorkspace.value : null)
    switch (workspace?.active_operation) {
      case WorkspaceOperation.CREATING:
        return 'Creating…'
      case WorkspaceOperation.STARTING:
        return 'Starting…'
      case WorkspaceOperation.STOPPING:
        return 'Stopping…'
      case WorkspaceOperation.RESTARTING:
        return 'Restarting…'
      case WorkspaceOperation.REMOVING:
        return 'Removing…'
      case WorkspaceOperation.CAPTURING_IMAGE:
        return 'Capturing image…'
    }

    const pending = pendingWorkspaceOperations.value[workspaceId]
    if (!pending) return null
    switch (pending.operation) {
      case 'create':
        return 'Creating…'
      case 'start':
        return 'Starting…'
      case 'stop':
        return 'Stopping…'
      case 'remove':
        return 'Removing…'
      default:
        return null
    }
  }

  function reconcilePendingWorkspaceOperation(
    workspaceId: string,
    status: WorkspaceStatus,
    previousStatus?: WorkspaceStatus,
  ): void {
    const notifications = useNotificationStore()
    const pending = pendingWorkspaceOperations.value[workspaceId]
    if (!pending) return
    if (previousStatus && previousStatus === status) return

    if (status === pending.expectedStatus) {
      const workspaceName = getWorkspaceName(workspaceId)
      switch (pending.operation) {
        case 'create':
          notifications.success('Workspace ready', `${workspaceName} is now running.`)
          break
        case 'start':
          notifications.success('Workspace started', `${workspaceName} is now running.`)
          break
        case 'stop':
          notifications.success('Workspace stopped', `${workspaceName} is now stopped.`)
          break
        case 'remove':
          notifications.success('Workspace removed', `${workspaceName} was removed.`)
          break
      }
      clearPendingWorkspaceOperation(workspaceId)
      return
    }

    if (status === WorkspaceStatus.FAILED || status === WorkspaceStatus.DELETE_FAILED) {
      const workspaceName = getWorkspaceName(workspaceId)
      switch (pending.operation) {
        case 'create':
          notifications.error('Creation failed', `${workspaceName} failed to start.`)
          break
        case 'start':
          notifications.error('Start failed', `${workspaceName} could not be started.`)
          break
        case 'stop':
          notifications.error('Stop failed', `${workspaceName} could not be stopped.`)
          break
        case 'remove':
          notifications.error('Removal failed', `${workspaceName} could not be removed.`)
          break
      }
      clearPendingWorkspaceOperation(workspaceId)
    }
  }

  async function fetchWorkspaces(runnerId?: string): Promise<void> {
    const notifications = useNotificationStore()
    loading.value = true
    error.value = null
    try {
      const previousStatuses = new Map(
        workspaces.value.map((workspace) => [workspace.id, workspace.status]),
      )
      const previousWorkspaceIds = new Set(workspaces.value.map((workspace) => workspace.id))
      workspaces.value = await workspacesApi.listWorkspaces(runnerId)

      const currentWorkspaceIds = new Set(workspaces.value.map((workspace) => workspace.id))

      for (const workspace of workspaces.value) {
        reconcilePendingWorkspaceOperation(
          workspace.id,
          workspace.status,
          previousStatuses.get(workspace.id),
        )
      }

      for (const workspaceId of previousWorkspaceIds) {
        const pending = pendingWorkspaceOperations.value[workspaceId]
        if (!pending) continue
        if (pending.operation === 'remove' && !currentWorkspaceIds.has(workspaceId)) {
          notifications.success('Workspace removed', 'The workspace was removed successfully.')
          clearPendingWorkspaceOperation(workspaceId)
        }
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load workspaces'
    } finally {
      loading.value = false
    }
  }

  async function fetchWorkspaceDetail(id: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const fresh = await workspacesApi.getWorkspace(id)

      if (activeWorkspace.value?.id === fresh.id) {
        activeWorkspace.value.status = fresh.status
        activeWorkspace.value.active_operation = fresh.active_operation
        activeWorkspace.value.name = fresh.name
        activeWorkspace.value.runtime_type = fresh.runtime_type
        activeWorkspace.value.qemu_vcpus = fresh.qemu_vcpus
        activeWorkspace.value.qemu_memory_mb = fresh.qemu_memory_mb
        activeWorkspace.value.qemu_disk_size_gb = fresh.qemu_disk_size_gb
        activeWorkspace.value.last_activity_at = fresh.last_activity_at
        activeWorkspace.value.auto_stop_timeout_minutes = fresh.auto_stop_timeout_minutes
        activeWorkspace.value.auto_stop_at = fresh.auto_stop_at
        activeWorkspace.value.updated_at = fresh.updated_at
        activeWorkspace.value.has_active_session = fresh.has_active_session
        activeWorkspace.value.runner_online = fresh.runner_online
        activeWorkspace.value.credential_ids = fresh.credential_ids
      } else {
        activeWorkspace.value = fresh
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load workspace'
    } finally {
      loading.value = false
    }
  }

  function mergeSessions(freshSessions: Session[]): void {
    const existingMap = new Map(activeSessions.value.map((session) => [session.id, session]))
    const merged: Session[] = []

    for (const freshSession of freshSessions) {
      const existing = existingMap.get(freshSession.id)
      if (existing) {
        if (existing.prompt !== freshSession.prompt) existing.prompt = freshSession.prompt
        if (existing.output !== freshSession.output) existing.output = freshSession.output
        if (existing.error_message !== freshSession.error_message) {
          existing.error_message = freshSession.error_message
        }
        if (existing.status !== freshSession.status) existing.status = freshSession.status
        if (existing.read_at !== freshSession.read_at) existing.read_at = freshSession.read_at
        if (existing.completed_at !== freshSession.completed_at) {
          existing.completed_at = freshSession.completed_at
        }
        if (existing.agent_model !== freshSession.agent_model) {
          existing.agent_model = freshSession.agent_model
        }
        if (existing.chat_id !== freshSession.chat_id) existing.chat_id = freshSession.chat_id
        if (existing.agent_options !== freshSession.agent_options) {
          existing.agent_options = freshSession.agent_options
        }
        if (existing.skills !== freshSession.skills) existing.skills = freshSession.skills
        merged.push(existing)
      } else {
        merged.push(freshSession)
      }
    }

    activeSessions.value = merged
  }

  async function fetchChatSessions(workspaceId: string, chatId: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const freshSessions = await workspacesApi.getChatSessions(workspaceId, chatId)
      mergeSessions(
        freshSessions.map((session) => ({
          ...session,
          workspace_id: workspaceId,
        })),
      )
    } catch (e: unknown) {
      activeSessions.value = []
      error.value = e instanceof Error ? e.message : 'Failed to load chat sessions'
    } finally {
      loading.value = false
    }
  }

  async function createWorkspace(data: WorkspaceCreateIn): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const result = await workspacesApi.createWorkspace(data)
      setPendingWorkspaceOperation(result.workspace_id, 'create', WorkspaceStatus.RUNNING)
      notifications.info('Workspace creating', 'Provisioning started. This can take a moment.')
      await fetchWorkspaces()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create workspace'
      notifications.error('Creation failed', msg)
      return false
    }
  }

  function applyWorkspaceUpdate(
    id: string,
    updated: {
      name: string
      updated_at: string
      active_operation: WorkspaceOperation | null
      credential_ids: string[]
      qemu_vcpus: number | null
      qemu_memory_mb: number | null
      qemu_disk_size_gb: number | null
    },
  ): void {
    const ws = workspaces.value.find((w) => w.id === id)
    if (ws) {
      ws.name = updated.name
      ws.updated_at = updated.updated_at
      ws.active_operation = updated.active_operation
      ws.credential_ids = updated.credential_ids
      ws.qemu_vcpus = updated.qemu_vcpus
      ws.qemu_memory_mb = updated.qemu_memory_mb
      ws.qemu_disk_size_gb = updated.qemu_disk_size_gb
    }

    if (activeWorkspace.value?.id === id) {
      activeWorkspace.value.name = updated.name
      activeWorkspace.value.updated_at = updated.updated_at
      activeWorkspace.value.active_operation = updated.active_operation
      activeWorkspace.value.credential_ids = updated.credential_ids
      activeWorkspace.value.qemu_vcpus = updated.qemu_vcpus
      activeWorkspace.value.qemu_memory_mb = updated.qemu_memory_mb
      activeWorkspace.value.qemu_disk_size_gb = updated.qemu_disk_size_gb
    }
  }

  async function updateWorkspace(id: string, data: WorkspaceUpdateIn): Promise<boolean> {
    const notifications = useNotificationStore()

    if (data.name !== undefined && !data.name.trim()) {
      notifications.error('Update failed', 'Workspace name must not be empty.')
      return false
    }

    try {
      const payload: WorkspaceUpdateIn = {
        ...(data.name !== undefined ? { name: data.name.trim() } : {}),
        ...(data.credential_ids !== undefined ? { credential_ids: data.credential_ids } : {}),
        ...(data.qemu_vcpus !== undefined ? { qemu_vcpus: data.qemu_vcpus } : {}),
        ...(data.qemu_memory_mb !== undefined ? { qemu_memory_mb: data.qemu_memory_mb } : {}),
        ...(data.qemu_disk_size_gb !== undefined ? { qemu_disk_size_gb: data.qemu_disk_size_gb } : {}),
      }

      const updated = await workspacesApi.updateWorkspace(id, payload)
      applyWorkspaceUpdate(id, updated)
      if (updated.active_operation === WorkspaceOperation.RESTARTING) {
        notifications.info('Workspace restarting', `${getWorkspaceName(id)} is restarting to apply the new resources.`)
      } else {
        notifications.success('Workspace updated', 'The workspace settings were saved.')
      }
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update workspace'
      notifications.error('Update failed', msg)
      return false
    }
  }

  async function renameWorkspace(id: string, name: string): Promise<boolean> {
    const trimmed = name.trim()
    if (!trimmed) {
      const notifications = useNotificationStore()
      notifications.error('Rename failed', 'Workspace name must not be empty.')
      return false
    }

    return updateWorkspace(id, { name: trimmed })
  }

  async function stopWorkspace(id: string): Promise<void> {
    const notifications = useNotificationStore()
    if (isWorkspaceTransitioning(id)) {
      notifications.info('Action already running', 'Please wait until the current workspace action finishes.')
      return
    }
    try {
      await workspacesApi.stopWorkspace(id)
      setPendingWorkspaceOperation(id, 'stop', WorkspaceStatus.STOPPED)
      notifications.info('Stopping workspace', `${getWorkspaceName(id)} is stopping…`)
      await fetchWorkspaces()
    } catch (e: unknown) {
      notifications.error('Stop failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function resumeWorkspace(id: string): Promise<void> {
    const notifications = useNotificationStore()
    if (isWorkspaceTransitioning(id)) {
      notifications.info('Action already running', 'Please wait until the current workspace action finishes.')
      return
    }
    try {
      await workspacesApi.resumeWorkspace(id)
      setPendingWorkspaceOperation(id, 'start', WorkspaceStatus.RUNNING)
      notifications.info('Starting workspace', `${getWorkspaceName(id)} is starting…`)
      await fetchWorkspaces()
    } catch (e: unknown) {
      notifications.error('Resume failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function removeWorkspace(id: string): Promise<void> {
    const notifications = useNotificationStore()
    if (isWorkspaceTransitioning(id)) {
      notifications.info('Action already running', 'Please wait until the current workspace action finishes.')
      return
    }
    try {
      await workspacesApi.deleteWorkspace(id)
      setPendingWorkspaceOperation(id, 'remove', WorkspaceStatus.DELETED)
      notifications.info('Removing workspace', `${getWorkspaceName(id)} is being removed…`)
      await fetchWorkspaces()
    } catch (e: unknown) {
      notifications.error('Removal failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  async function sendPrompt(
    workspaceId: string,
    prompt: string,
    agentOptions?: Record<string, string>,
    chatId?: string,
    skillIds?: string[],
  ): Promise<string | null> {
    const notifications = useNotificationStore()
    const skillStore = useSkillStore()
    try {
      // If the active chat is still a pending (draft) chat, persist it to the
      // backend now — before sending the first prompt.
      const effectiveChatId = chatId || activeChatId.value
      if (effectiveChatId === PENDING_CHAT_ID) {
        const pendingChat = chats.value.find((c) => c.is_pending)
        const chatName = prompt.slice(0, 50) + (prompt.length > 50 ? '…' : '')
        // Update the local pending chat name immediately so the sidebar shows it
        if (pendingChat) pendingChat.name = chatName
        const realChat = await _persistPendingChat(workspaceId, {
          name: chatName,
          agent_definition_id: pendingChat?.agent_definition_id || undefined,
        })
        if (!realChat) return null // creation failed — error already notified
      }

      const result = await workspacesApi.promptWorkspace(workspaceId, {
        prompt,
        agent_options: agentOptions && Object.keys(agentOptions).length ? agentOptions : undefined,
        chat_id: chatId || activeChatId.value || undefined,
        skill_ids: skillIds?.length ? skillIds : undefined,
      })

      // Mark the workspace as busy immediately (optimistic update)
      const busyWs = workspaces.value.find((w) => w.id === workspaceId)
      if (busyWs) busyWs.has_active_session = true
      if (activeWorkspace.value?.id === workspaceId) {
        activeWorkspace.value.has_active_session = true
      }

      // Add a pending session to the active workspace immediately
      if (activeWorkspace.value && activeWorkspace.value.id === workspaceId) {
        const optimisticSkills: SessionSkill[] = (skillIds ?? [])
          .map((id) => skillStore.skills.find((s: Skill) => s.id === id))
          .filter((s): s is Skill => s !== undefined)
          .map((s: Skill) => ({
            id: s.id,
            skill_id: s.id,
            name: s.name,
            body: s.body,
            created_at: new Date().toISOString(),
          }))
        const newSession: Session = {
          id: result.session_id,
          workspace_id: workspaceId,
          chat_id: result.chat_id || null,
          prompt,
          agent_model: agentOptions?.['model'] || '',
          agent_options: agentOptions ?? {},
          output: '',
          error_message: null,
          status: SessionStatus.PENDING,
          read_at: null,
          status_detail: 'Preparing agent execution…',
          created_at: new Date().toISOString(),
          completed_at: null,
          skills: optimisticSkills,
        }
        activeSessions.value.push(newSession)

        // If this created/used a chat, make sure it's the active one
        if (result.chat_id && result.chat_id !== activeChatId.value) {
          activeChatId.value = result.chat_id
          // Refresh chats to pick up new chat
          await fetchChats(workspaceId)
        }
      }

      return result.session_id
    } catch (e: unknown) {
      notifications.error('Prompt failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  async function cancelActivePrompt(workspaceId: string): Promise<void> {
    const notifications = useNotificationStore()
    if (!activeWorkspace.value || activeWorkspace.value.id !== workspaceId) {
      notifications.error('Cancel failed', 'Workspace details are not loaded.')
      return
    }

    const activeSession = [...activeSessions.value]
      .reverse()
      .find(
        (session) => isSessionActive(session.status),
      )

    if (!activeSession) {
      notifications.info('No active session', 'There is no running completion to stop.')
      return
    }

    try {
      await workspacesApi.cancelSessionPrompt(workspaceId, activeSession.id)
      activeSession.status_detail = 'Cancellation requested…'
      notifications.info('Stopping completion', 'The running agent process is being stopped…')
    } catch (e: unknown) {
      notifications.error('Cancel failed', e instanceof Error ? e.message : 'Unknown error')
    }
  }

  // --- Chat actions ---

  async function fetchChats(workspaceId: string): Promise<void> {
    try {
      const fetched = await workspacesApi.listChats(workspaceId)
      // Preserve any pending (not-yet-saved) chat at the top of the list
      const pending = chats.value.find((c) => c.is_pending)
      chats.value = pending ? [pending, ...fetched] : fetched
      const activeChatExists = activeChatId.value
        ? chats.value.some((chat) => chat.id === activeChatId.value)
        : false
      if (!activeChatExists) {
        setActiveChat(chats.value[0]?.id ?? null)
      }
    } catch {
      chats.value = []
      activeChatId.value = null
      activeSessions.value = []
    }
  }

  function setActiveChat(chatId: string | null): void {
    // Discard any pending chat when the user navigates to a real chat
    if (chatId !== PENDING_CHAT_ID) {
      chats.value = chats.value.filter((c) => !c.is_pending)
    }
    if (activeChatId.value !== chatId) {
      activeSessions.value = []
    }
    activeChatId.value = chatId
  }

  /**
   * Creates a local draft chat (not yet persisted to the backend).
   * The chat will be created on the backend when the first message is sent.
   */
  function createChat(
    workspaceId: string,
    options?: { name?: string; agent_definition_id?: string; agent_type?: string },
  ): Chat {
    // Discard any existing pending chat first
    chats.value = chats.value.filter((c) => !c.is_pending)

    const now = new Date().toISOString()
    const pendingChat: Chat = {
      id: PENDING_CHAT_ID,
      workspace_id: workspaceId,
      name: options?.name ?? 'New Chat',
      agent_definition_id: options?.agent_definition_id ?? null,
      agent_type: options?.agent_type ?? '',
      created_at: now,
      updated_at: now,
      session_count: 0,
      is_pending: true,
    }
    chats.value.unshift(pendingChat)
    activeChatId.value = PENDING_CHAT_ID
    return pendingChat
  }

  /**
   * Persist a pending chat to the backend immediately.
   * Called internally when the first message is sent.
   */
  async function _persistPendingChat(
    workspaceId: string,
    options?: { name?: string; agent_definition_id?: string },
  ): Promise<Chat | null> {
    const notifications = useNotificationStore()
    try {
      const chat = await workspacesApi.createChat(workspaceId, options)
      // Replace the pending placeholder with the real chat
      const idx = chats.value.findIndex((c) => c.is_pending)
      if (idx !== -1) {
        chats.value[idx] = chat
      } else {
        chats.value.unshift(chat)
      }
      activeChatId.value = chat.id
      return chat
    } catch (e: unknown) {
      notifications.error('Chat creation failed', e instanceof Error ? e.message : 'Unknown error')
      return null
    }
  }

  async function renameChatAction(
    workspaceId: string,
    chatId: string,
    name: string,
  ): Promise<boolean> {
    // Pending chats haven't been persisted yet — just update locally
    if (chatId === PENDING_CHAT_ID) {
      const idx = chats.value.findIndex((c) => c.is_pending)
      if (idx !== -1) chats.value[idx]!.name = name
      return true
    }

    const notifications = useNotificationStore()
    try {
      const updated = await workspacesApi.renameChat(workspaceId, chatId, { name })
      const idx = chats.value.findIndex((c) => c.id === chatId)
      if (idx !== -1) {
        chats.value[idx] = updated
      }
      return true
    } catch (e: unknown) {
      notifications.error('Rename failed', e instanceof Error ? e.message : 'Unknown error')
      return false
    }
  }

  async function deleteChatAction(workspaceId: string, chatId: string): Promise<boolean> {
    const notifications = useNotificationStore()

    // Pending chats only exist locally — no backend call needed
    if (chatId === PENDING_CHAT_ID) {
      chats.value = chats.value.filter((c) => c.id !== chatId)
      if (activeChatId.value === chatId) {
        activeChatId.value = chats.value[0]?.id || null
      }
      return true
    }

    try {
      await workspacesApi.deleteChat(workspaceId, chatId)
      chats.value = chats.value.filter((c) => c.id !== chatId)

      if (activeChatId.value === chatId) activeSessions.value = []

      // Switch to another chat if the active one was deleted
      if (activeChatId.value === chatId) {
        activeChatId.value = chats.value[0]?.id || null
      }

      notifications.success('Chat deleted', 'The chat was removed.')
      return true
    } catch (e: unknown) {
      notifications.error('Delete failed', e instanceof Error ? e.message : 'Unknown error')
      return false
    }
  }

  function setSupportsMultiChat(value: boolean): void {
    supportsMultiChat.value = value
  }

  // --- Image capture actions ---

  async function fetchImageArtifacts(workspaceId: string): Promise<void> {
    try {
      imageArtifacts.value = await workspacesApi.listWorkspaceImageArtifacts(workspaceId)
    } catch {
      imageArtifacts.value = []
    }
  }

  async function createImageArtifact(
    workspaceId: string,
    data: ImageArtifactCreateIn,
  ): Promise<boolean> {
    const notifications = useNotificationStore()
    const imageStore = useImageStore()
    try {
      await workspacesApi.createWorkspaceImageArtifact(workspaceId, data)
      await imageStore.fetchImages()
      notifications.success('Image capturing', 'Image is being captured.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to capture image'
      notifications.error('Image capture failed', msg)
      return false
    }
  }

  async function deleteImageArtifact(
    workspaceId: string,
    imageArtifactId: string,
  ): Promise<boolean> {
    const notifications = useNotificationStore()
    const imageStore = useImageStore()
    try {
      await workspacesApi.deleteWorkspaceImageArtifact(workspaceId, imageArtifactId)
      imageArtifacts.value = imageArtifacts.value.filter((artifact) => artifact.id !== imageArtifactId)
      await imageStore.fetchImages()
      notifications.success('Image deleted', 'The image was removed.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to delete image'
      notifications.error('Delete failed', msg)
      return false
    }
  }

  async function createWorkspaceFromImageArtifact(
    workspaceId: string,
    imageArtifactId: string,
    data: ImageArtifactCloneIn,
  ): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const result = await workspacesApi.createWorkspaceFromImageArtifact(
        workspaceId,
        imageArtifactId,
        data,
      )
      setPendingWorkspaceOperation(result.workspace_id, 'create', WorkspaceStatus.RUNNING)
      notifications.info('Cloning workspace', 'The cloned workspace is being provisioned.')
      await fetchWorkspaces()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to clone workspace'
      notifications.error('Clone failed', msg)
      return false
    }
  }

  // --- Real-time handlers ---

  function updateWorkspaceStatus(workspaceId: string, status: WorkspaceStatus): void {
    // Update in list
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    const previousStatus = ws?.status ?? (activeWorkspace.value?.id === workspaceId
      ? activeWorkspace.value.status
      : undefined)
    if (ws) {
      ws.status = status
      if (status === WorkspaceStatus.STOPPED) {
        ws.auto_stop_at = null
      }
    }

    // Update active workspace
    if (activeWorkspace.value?.id === workspaceId) {
      activeWorkspace.value.status = status
      if (status === WorkspaceStatus.STOPPED) {
        activeWorkspace.value.auto_stop_at = null
      }
    }
    reconcilePendingWorkspaceOperation(workspaceId, status, previousStatus)
  }

  function updateWorkspaceOperation(
    workspaceId: string,
    activeOperation: WorkspaceOperation | null,
  ): void {
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (ws) ws.active_operation = activeOperation

    if (activeWorkspace.value?.id === workspaceId) {
      activeWorkspace.value.active_operation = activeOperation
    }

    if (activeOperation === null) {
      clearPendingWorkspaceOperation(workspaceId)
    }
  }

  /** Update runner_online flag for a single workspace. */
  function updateWorkspaceRunnerOnline(workspaceId: string, online: boolean): void {
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (ws) ws.runner_online = online

    if (activeWorkspace.value?.id === workspaceId) {
      activeWorkspace.value.runner_online = online
    }
  }

  function appendOutputChunk(sessionId: string, chunk: string): void {
    const session = activeSessions.value.find((s) => s.id === sessionId)
    if (session) {
      // Mirror the backend's append_output logic: join chunks with "\n"
      // so the live-streamed output matches what is stored in the database.
      if (session.output) {
        session.output += '\n' + chunk
      } else {
        session.output = chunk
      }
      if (session.status === SessionStatus.PENDING) {
        session.status = SessionStatus.RUNNING
      }
    }
  }

  function setSessionStatusDetail(sessionId: string, detail: string): void {
    const session = activeSessions.value.find((s) => s.id === sessionId)
    if (!session) return
    session.status_detail = detail
  }

  function _refreshActiveSessionFlag(): void {
    if (!activeWorkspace.value) return
    const workspaceId = activeWorkspace.value.id
    const stillActive = activeSessions.value.some(
      (s) => isSessionActive(s.status),
    )
    const ws = workspaces.value.find((w) => w.id === workspaceId)
    if (ws) ws.has_active_session = stillActive
    activeWorkspace.value.has_active_session = stillActive
  }

  function markSessionComplete(sessionId: string): void {
    const session = activeSessions.value.find((s) => s.id === sessionId)
    if (session) {
      session.status = SessionStatus.COMPLETED
      session.read_at = null
      session.status_detail = undefined
      session.error_message = null
      session.completed_at = new Date().toISOString()
    }
    _refreshActiveSessionFlag()
  }

  function markSessionFailed(sessionId: string, errorMsg?: string): void {
    const session = activeSessions.value.find((s) => s.id === sessionId)
    if (session) {
      session.status = SessionStatus.FAILED
      session.read_at = null
      session.status_detail = undefined
      session.error_message = errorMsg?.trim() || 'The last agent response or command execution failed.'
      if (errorMsg) session.output += `\n[Error] ${errorMsg}`
    }
    _refreshActiveSessionFlag()
  }

  function handleWorkspaceError(workspaceId: string, errorMsg: string): void {
    const notifications = useNotificationStore()
    const workspaceName = getWorkspaceName(workspaceId)
    notifications.error('Workspace error', `${workspaceName}: ${errorMsg}`)
    updateWorkspaceOperation(workspaceId, null)
    clearPendingWorkspaceOperation(workspaceId)
  }

  return {
    // State
    workspaces,
    activeWorkspace,
    activeSessions,
    loading,
    error,
    chats,
    activeChatId,
    supportsMultiChat,
    imageArtifacts,
    pendingWorkspaceOperations,
    // Getters
    runningWorkspaces,
    workspacesByStatus,
    activeChatSessions,
    activeChat,
    // Actions
    fetchWorkspaces,
    fetchWorkspaceDetail,
    fetchChatSessions,
    createWorkspace,
    updateWorkspace,
    renameWorkspace,
    stopWorkspace,
    resumeWorkspace,
    removeWorkspace,
    sendPrompt,
    cancelActivePrompt,
    // Chat actions
    fetchChats,
    setActiveChat,
    createChat,
    renameChatAction,
    deleteChatAction,
    setSupportsMultiChat,
    // Image artifact actions
    fetchImageArtifacts,
    createImageArtifact,
    deleteImageArtifact,
    createWorkspaceFromImageArtifact,
    // Real-time
    isWorkspaceTransitioning,
    getWorkspaceTransitionLabel,
    updateWorkspaceStatus,
    updateWorkspaceOperation,
    updateWorkspaceRunnerOnline,
    appendOutputChunk,
    setSessionStatusDetail,
    markSessionComplete,
    markSessionFailed,
    handleWorkspaceError,
  }
})
