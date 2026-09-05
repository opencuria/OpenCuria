/**
 * Workspace Pinia store.
 *
 * Manages workspace lifecycle state. Agent conversations live in the
 * harness store (`stores/harness.ts`) as HarnessSessions.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import type {
  Workspace,
  WorkspaceDetail,
  WorkspaceCreateIn,
  WorkspaceUpdateIn,
  ImageArtifact,
  ImageArtifactCreateIn,
  ImageArtifactCloneIn,
} from '@/types'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { useNotificationStore } from './notifications'
import { useImageStore } from './images'

type PendingWorkspaceOperationType = 'create' | 'start' | 'stop' | 'remove'

interface PendingWorkspaceOperation {
  operation: PendingWorkspaceOperationType
  expectedStatus: WorkspaceStatus
}

export const useWorkspaceStore = defineStore('workspaces', () => {
  // --- State ---
  const workspaces = ref<Workspace[]>([])
  const activeWorkspace = ref<WorkspaceDetail | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
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
    removed: workspaces.value.filter((w) => w.status === WorkspaceStatus.REMOVED),
    pending_deletion: workspaces.value.filter((w) => w.status === WorkspaceStatus.PENDING_DELETION),
    deleting: workspaces.value.filter((w) => w.status === WorkspaceStatus.DELETING),
    deleted: workspaces.value.filter((w) => w.status === WorkspaceStatus.DELETED),
    delete_failed: workspaces.value.filter((w) => w.status === WorkspaceStatus.DELETE_FAILED),
  }))

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
      WorkspaceStatus.REMOVED,
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

    const removeCompleted =
      pending.operation === 'remove' &&
      (status === WorkspaceStatus.REMOVED || status === WorkspaceStatus.DELETED)
    if (status === pending.expectedStatus || removeCompleted) {
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
    loading,
    error,
    imageArtifacts,
    pendingWorkspaceOperations,
    // Getters
    runningWorkspaces,
    workspacesByStatus,
    // Actions
    fetchWorkspaces,
    fetchWorkspaceDetail,
    createWorkspace,
    updateWorkspace,
    renameWorkspace,
    stopWorkspace,
    resumeWorkspace,
    removeWorkspace,
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
    handleWorkspaceError,
  }
})
