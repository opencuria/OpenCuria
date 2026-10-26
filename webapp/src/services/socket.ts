/** Browser state and frontend commands over the shared realtime transport. */
import { RealtimeClient } from '@opencuria/client/realtime'
import { planFilesUpload } from '@opencuria/chat-core/fileChunks'
import { getActivePinia } from 'pinia'
import { ref } from 'vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { getConfig } from './config'
import { getApiClient } from './api'
import type {
  FilesListResultEvent,
  FilesFindResultEvent,
  FilesContentResultEvent,
  FilesContentChunkEvent,
  FilesUploadResultEvent,
  FilesDownloadResultEvent,
  FilesDownloadChunkEvent,
  ProcessRemovedEvent,
  ProcessStatusChangedEvent,
  WorkspaceDiskUsage,
} from '@/types'
import type {
  HarnessPartUpdatedEvent,
  HarnessPermissionRequiredEvent,
  HarnessPermissionResolvedEvent,
  HarnessQuestionRequiredEvent,
  HarnessQuestionResolvedEvent,
  HarnessSessionStatusEvent,
  HarnessSessionSummaryEvent,
  HarnessSteerQueueEvent,
  HarnessSubtaskFinishedEvent,
  HarnessSubtaskStartedEvent,
  HarnessTodoUpdatedEvent,
} from '@/types/harness'

// Re-exported for socket listeners in harness views (avoids a second import).
export type {
  HarnessPartUpdatedEvent,
  HarnessPermissionRequiredEvent,
  HarnessPermissionResolvedEvent,
  HarnessQuestionRequiredEvent,
  HarnessQuestionResolvedEvent,
  HarnessSessionStatusEvent,
  HarnessSessionSummaryEvent,
  HarnessSteerQueueEvent,
  HarnessSubtaskFinishedEvent,
  HarnessSubtaskStartedEvent,
  HarnessTodoUpdatedEvent,
}

export const isConnected = ref(false)
let client: RealtimeClient | null = null

function getClient(): RealtimeClient {
  if (!client) {
    const api = getApiClient()
    client = new RealtimeClient({
      wsBaseUrl: getConfig().wsBaseUrl,
      getAccessToken: () => localStorage.getItem('kern_access_token'),
      getContextKey: () => {
        const context = api.getContext()
        return `${context.generation}:${context.baseUrl}:${context.organizationId ?? ''}`
      },
      onContextChange: (callback) => api.onContextChange(callback),
      refreshAccessToken: () => api.refreshAccessToken(),
    })
    client.on('connect', () => {
      isConnected.value = true
    })
    client.on('disconnect', () => {
      isConnected.value = false
    })
  }
  return client
}

function handleVisibilityChange(): void {
  if (document.visibilityState === 'visible' && client && !client.isConnected) client.connect()
}

// ---------------------------------------------------------------------------
// Event callback types
// ---------------------------------------------------------------------------

export interface WorkspaceStatusEvent {
  workspace_id: string
  status: string
  task_id?: string
  credentials_present?: boolean
}

export interface WorkspaceDiskUsageEvent {
  workspace_id: string
  disk_usage: WorkspaceDiskUsage
}

export interface WorkspaceOperationEvent {
  workspace_id: string
  active_operation: string | null
  intervention_required?: boolean
  lifecycle_diagnostic?: string
}

export interface WorkspaceErrorEvent {
  workspace_id: string
  task_id: string
  error: string
}

export interface TerminalStartedEvent {
  workspace_id: string
  terminal_id: string
  task_id: string
}

export interface TerminalOutputEvent {
  workspace_id: string
  terminal_id: string
  data: string // base64-encoded bytes
}

export interface TerminalClosedEvent {
  workspace_id: string
  terminal_id: string
}

export interface DesktopStartedEvent {
  workspace_id: string
  task_id: string
  proxy_url: string
  computer_use_active?: boolean
}

export interface DesktopStoppedEvent {
  workspace_id: string
  task_id: string
}

export interface DesktopViewerReleasedEvent {
  workspace_id: string
  task_id: string
  computer_use_active: boolean
}

export interface RunnerOfflineEvent {
  workspace_id: string
  runner_id: string
}

export interface RunnerOnlineEvent {
  workspace_id: string
  runner_id: string
}

// Event name → payload type mapping
type EventMap = {
  'workspace:status_changed': WorkspaceStatusEvent
  'workspace:operation_changed': WorkspaceOperationEvent
  'workspace:disk_usage_changed': WorkspaceDiskUsageEvent
  'workspace:error': WorkspaceErrorEvent
  'harness.part_updated': HarnessPartUpdatedEvent
  'harness.permission_required': HarnessPermissionRequiredEvent
  'harness.question_required': HarnessQuestionRequiredEvent
  'harness.session_status': HarnessSessionStatusEvent
  'harness.session_summary': HarnessSessionSummaryEvent
  'harness.conversations_changed': { workspace_id: string }
  'harness.todo_updated': HarnessTodoUpdatedEvent
  'harness.steer_queue': HarnessSteerQueueEvent
  'harness.subtask_started': HarnessSubtaskStartedEvent
  'harness.subtask_finished': HarnessSubtaskFinishedEvent
  'terminal:started': TerminalStartedEvent
  'terminal:output': TerminalOutputEvent
  'terminal:closed': TerminalClosedEvent
  'desktop:started': DesktopStartedEvent
  'desktop:stopped': DesktopStoppedEvent
  'desktop:viewer_released': DesktopViewerReleasedEvent
  'files:list_result': FilesListResultEvent
  'files:find_result': FilesFindResultEvent
  'files:content_result': FilesContentResultEvent
  'files:content_chunk': FilesContentChunkEvent
  'files:upload_result': FilesUploadResultEvent
  'files:download_result': FilesDownloadResultEvent
  'files:download_chunk': FilesDownloadChunkEvent
  'runner:offline': RunnerOfflineEvent
  'runner:online': RunnerOnlineEvent
  'process:status_changed': ProcessStatusChangedEvent
  'process:removed': ProcessRemovedEvent
}

type EventName = keyof EventMap

/** Connect the shared transport and resume it when the browser tab becomes visible. */
export function connect(): void {
  document.addEventListener('visibilitychange', handleVisibilityChange)
  getClient().connect()
}

/** Release the transport and all subscriptions on logout or organization switch. */
export function disconnect(): void {
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  client?.dispose()
  client = null
  isConnected.value = false
}

/** Hold a workspace room until the returned release callback is called. */
export function subscribeToWorkspace(workspaceId: string): () => void {
  return getClient().subscribeWorkspace(workspaceId)
}

/** Register a typed frontend event listener. */
export function onEvent<E extends EventName>(
  event: E,
  callback: (data: EventMap[E]) => void,
): () => void {
  return getClient().on(event, callback as (data: unknown) => void)
}

/** Refresh mounted views after an ordinary transport reconnection. */
export function onReconnect(callback: () => void): () => void {
  return getClient().onReconnect(callback)
}

// ---------------------------------------------------------------------------
// Terminal helpers
// ---------------------------------------------------------------------------

/**
 * Send terminal stdin input to the backend (base64-encoded).
 */
export function sendTerminalInput(workspaceId: string, terminalId: string, data: string): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  client?.emit('frontend:terminal_input', {
    workspace_id: workspaceId,
    terminal_id: terminalId,
    data,
  })
}

/**
 * Notify the backend of a terminal resize.
 */
export function sendTerminalResize(
  workspaceId: string,
  terminalId: string,
  cols: number,
  rows: number,
): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  client?.emit('frontend:terminal_resize', {
    workspace_id: workspaceId,
    terminal_id: terminalId,
    cols,
    rows,
  })
}

/**
 * Request terminal close.
 */
export function sendTerminalClose(workspaceId: string, terminalId: string): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  client?.emit('frontend:terminal_close', {
    workspace_id: workspaceId,
    terminal_id: terminalId,
  })
}

// ---------------------------------------------------------------------------
// File explorer helpers
// ---------------------------------------------------------------------------

/**
 * Request a directory listing inside the workspace VM.
 */
export function sendFilesList(workspaceId: string, requestId: string, path: string): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  client?.emit('frontend:files_list', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
  })
}

/**
 * Search workspace files for `@` mention autocomplete.
 *
 * Returns false when the socket is not connected so callers can resolve
 * immediately instead of waiting for a result that will never arrive.
 */
export function sendFilesFind(
  workspaceId: string,
  requestId: string,
  query: string,
  limit: number,
): boolean {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return false
  return (
    client?.emit('frontend:files_find', {
      workspace_id: workspaceId,
      request_id: requestId,
      query,
      limit,
    }) ?? false
  )
}

/**
 * Request file content (base64-encoded) from the workspace VM.
 */
export function sendFilesRead(
  workspaceId: string,
  requestId: string,
  path: string,
  maxSize?: number,
): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  client?.emit('frontend:files_read', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
    max_size: maxSize,
  })
}

/**
 * Upload a file to the workspace VM.
 *
 * Uses the shared start/chunk/finish plan for every file size.
 *
 * Throws before emitting any event when the payload exceeds
 * MAX_UPLOAD_CHUNKS_PER_TRANSFER chunks or UPLOAD_MAX_BYTES exact raw
 * bytes, so an oversize upload never emits a partial stream.
 */
export function sendFilesUpload(
  workspaceId: string,
  requestId: string,
  path: string,
  filename: string,
  content: string,
  isDirectory: boolean = false,
): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  if (!client) return
  for (const planned of planFilesUpload(
    workspaceId,
    requestId,
    path,
    filename,
    content,
    isDirectory,
  )) {
    client.emit(planned.event, planned.payload)
  }
}

/**
 * Download a file or directory from the workspace VM.
 */
export function sendFilesDownload(workspaceId: string, requestId: string, path: string): void {
  if (getActivePinia() && useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
  client?.emit('frontend:files_download', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
  })
}
