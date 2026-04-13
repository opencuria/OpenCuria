/**
 * Socket.IO client for real-time frontend updates.
 *
 * Connects to the backend's /frontend namespace to receive
 * workspace status changes and streaming prompt output.
 *
 * The client is a singleton — call `connect()` once on app init
 * and `disconnect()` on teardown.
 */

import { io, type Socket } from 'socket.io-client'
import { ref } from 'vue'
import { getConfig } from './config'
import { tryRefreshToken } from './api'
import type {
  FilesListResultEvent,
  FilesContentResultEvent,
  FilesUploadResultEvent,
  FilesDownloadResultEvent,
} from '@/types'

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let socket: Socket | null = null

/** Whether the socket has ever connected (used to distinguish reconnect from initial connect). */
let hasConnectedOnce = false

/** Workspace IDs currently subscribed — auto-resubscribed on reconnect. */
const subscribedWorkspaces = new Set<string>()

/** Callbacks fired on every socket reconnection (not the initial connect). */
const reconnectCallbacks = new Set<() => void>()

export const isConnected = ref(false)

/** Whether the Page Visibility listener has been registered. */
let visibilityListenerAdded = false

// ---------------------------------------------------------------------------
// Page Visibility — reconnect when the tab comes back into focus
// ---------------------------------------------------------------------------

function handleVisibilityChange(): void {
  if (document.visibilityState === 'visible' && socket && !socket.connected) {
    console.debug('[socket] page became visible — triggering reconnect')
    socket.connect()
  }
}

// ---------------------------------------------------------------------------
// Event callback types
// ---------------------------------------------------------------------------

export interface WorkspaceStatusEvent {
  workspace_id: string
  status: string
  task_id?: string
}

export interface WorkspaceOperationEvent {
  workspace_id: string
  active_operation: string | null
}

export interface OutputChunkEvent {
  workspace_id: string
  session_id: string
  chat_id: string | null
  task_id: string
  line: string
}

export interface SessionCompleteEvent {
  workspace_id: string
  session_id: string
  chat_id: string | null
  task_id: string
}

export interface SessionFailedEvent {
  workspace_id: string
  session_id: string
  chat_id: string | null
  task_id: string
  error: string
}

export interface SessionStatusEvent {
  workspace_id: string
  session_id: string
  chat_id: string | null
  task_id: string
  status: string
  detail: string
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
}

export interface DesktopStoppedEvent {
  workspace_id: string
  task_id: string
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
  'workspace:error': WorkspaceErrorEvent
  'session:output_chunk': OutputChunkEvent
  'session:status': SessionStatusEvent
  'session:completed': SessionCompleteEvent
  'session:failed': SessionFailedEvent
  'terminal:started': TerminalStartedEvent
  'terminal:output': TerminalOutputEvent
  'terminal:closed': TerminalClosedEvent
  'desktop:started': DesktopStartedEvent
  'desktop:stopped': DesktopStoppedEvent
  'files:list_result': FilesListResultEvent
  'files:content_result': FilesContentResultEvent
  'files:upload_result': FilesUploadResultEvent
  'files:download_result': FilesDownloadResultEvent
  'runner:offline': RunnerOfflineEvent
  'runner:online': RunnerOnlineEvent
}

type EventName = keyof EventMap

// Listener storage for typed, removable subscriptions
const listeners = new Map<string, Set<(...args: unknown[]) => void>>()

// ---------------------------------------------------------------------------
// Connection management
// ---------------------------------------------------------------------------

/**
 * Connect to the backend Socket.IO server's /frontend namespace.
 */
export function connect(): void {
  // Register the Page Visibility listener exactly once.
  if (!visibilityListenerAdded) {
    document.addEventListener('visibilitychange', handleVisibilityChange)
    visibilityListenerAdded = true
  }

  // If a socket instance already exists, reuse it instead of creating a new one.
  // This handles the case where Socket.IO is already in the middle of reconnecting.
  if (socket) {
    if (!socket.connected) socket.connect()
    return
  }

  const config = getConfig()

  // In production, wsBaseUrl is a full URL (e.g. https://api.example.com).
  // In dev, it's empty and Socket.IO connects to the same origin (proxied by Vite).
  const baseUrl = config.wsBaseUrl || undefined

  socket = io(baseUrl ? `${baseUrl}/frontend` : '/frontend', {
    path: '/ws/runner',
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 10000,
    // Use a callback so every reconnect attempt reads the freshest token from
    // localStorage rather than the (possibly expired) token captured at init time.
    auth: (cb: (data: Record<string, string>) => void) => {
      const latestToken = localStorage.getItem('kern_access_token')
      cb(latestToken ? { token: latestToken } : {})
    },
  })

  socket.on('connect', () => {
    const isReconnect = hasConnectedOnce
    hasConnectedOnce = true
    isConnected.value = true
    console.debug('[socket] connected to /frontend namespace')

    if (isReconnect) {
      // Re-subscribe all workspaces after reconnect (server-side room memberships are lost on disconnect)
      console.debug('[socket] reconnected — re-subscribing', subscribedWorkspaces.size, 'workspace(s)')
      for (const wsId of subscribedWorkspaces) {
        socket?.emit('frontend:subscribe_workspace', { workspace_id: wsId })
      }
      // Notify registered reconnect callbacks (e.g. terminal)
      for (const cb of reconnectCallbacks) {
        cb()
      }
    }
  })

  socket.on('disconnect', () => {
    isConnected.value = false
    console.debug('[socket] disconnected')
  })

  socket.on('connect_error', async (err) => {
    console.warn('[socket] connection error:', err.message)

    // If the error looks auth-related, proactively refresh the JWT so the
    // next automatic reconnect attempt (handled by Socket.IO internally) will
    // use the fresh token via the auth callback above.
    const msg = err.message.toLowerCase()
    if (
      msg.includes('auth') ||
      msg.includes('401') ||
      msg.includes('unauthorized') ||
      msg.includes('forbidden')
    ) {
      console.debug('[socket] auth error — attempting token refresh')
      await tryRefreshToken()
      // Socket.IO will retry automatically; the auth callback will pick up the new token.
    }
  })
}

/**
 * Disconnect from Socket.IO.
 */
export function disconnect(): void {
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  visibilityListenerAdded = false
  socket?.disconnect()
  socket = null
  hasConnectedOnce = false
  isConnected.value = false
  listeners.clear()
  subscribedWorkspaces.clear()
  reconnectCallbacks.clear()
}

// ---------------------------------------------------------------------------
// Workspace subscriptions
// ---------------------------------------------------------------------------

/**
 * Tell the server to send events for a specific workspace.
 * The subscription is tracked and automatically restored on reconnect.
 */
export function subscribeToWorkspace(workspaceId: string): void {
  subscribedWorkspaces.add(workspaceId)
  socket?.emit('frontend:subscribe_workspace', { workspace_id: workspaceId })
}

/**
 * Stop receiving events for a specific workspace.
 */
export function unsubscribeFromWorkspace(workspaceId: string): void {
  subscribedWorkspaces.delete(workspaceId)
  socket?.emit('frontend:unsubscribe_workspace', { workspace_id: workspaceId })
}

// ---------------------------------------------------------------------------
// Typed event listeners
// ---------------------------------------------------------------------------

/**
 * Register a typed listener for a real-time event.
 * Returns an unsubscribe function.
 */
export function onEvent<E extends EventName>(
  event: E,
  callback: (data: EventMap[E]) => void,
): () => void {
  if (!socket) {
    console.warn('[socket] cannot listen — not connected')
    return () => {}
  }

  const handler = callback as (...args: unknown[]) => void
  socket.on(event as string, handler)

  // Track for cleanup
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(handler)

  return () => {
    socket?.off(event as string, handler)
    listeners.get(event)?.delete(handler)
  }
}

/**
 * Remove all registered listeners.
 */
export function removeAllListeners(): void {
  for (const [event, handlers] of listeners) {
    for (const handler of handlers) {
      socket?.off(event, handler)
    }
  }
  listeners.clear()
}

/**
 * Register a callback that fires on every socket reconnection (not the initial connect).
 * Useful for components that need to re-establish backend state after a reconnect.
 * Returns an unsubscribe function.
 */
export function onReconnect(callback: () => void): () => void {
  reconnectCallbacks.add(callback)
  return () => reconnectCallbacks.delete(callback)
}

// ---------------------------------------------------------------------------
// Terminal helpers
// ---------------------------------------------------------------------------

/**
 * Send terminal stdin input to the backend (base64-encoded).
 */
export function sendTerminalInput(
  workspaceId: string,
  terminalId: string,
  data: string,
): void {
  socket?.emit('frontend:terminal_input', {
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
  socket?.emit('frontend:terminal_resize', {
    workspace_id: workspaceId,
    terminal_id: terminalId,
    cols,
    rows,
  })
}

/**
 * Request terminal close.
 */
export function sendTerminalClose(
  workspaceId: string,
  terminalId: string,
): void {
  socket?.emit('frontend:terminal_close', {
    workspace_id: workspaceId,
    terminal_id: terminalId,
  })
}

// ---------------------------------------------------------------------------
// File explorer helpers
// ---------------------------------------------------------------------------

/**
 * Request a directory listing inside the workspace container.
 */
export function sendFilesList(
  workspaceId: string,
  requestId: string,
  path: string,
): void {
  socket?.emit('frontend:files_list', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
  })
}

/**
 * Request file content (base64-encoded) from the workspace container.
 */
export function sendFilesRead(
  workspaceId: string,
  requestId: string,
  path: string,
  maxSize?: number,
): void {
  socket?.emit('frontend:files_read', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
    max_size: maxSize,
  })
}

/**
 * Upload a file to the workspace container.
 */
export function sendFilesUpload(
  workspaceId: string,
  requestId: string,
  path: string,
  filename: string,
  content: string,
  isDirectory: boolean = false,
): void {
  socket?.emit('frontend:files_upload', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
    filename,
    content,
    is_directory: isDirectory,
  })
}

/**
 * Download a file or directory from the workspace container.
 */
export function sendFilesDownload(
  workspaceId: string,
  requestId: string,
  path: string,
): void {
  socket?.emit('frontend:files_download', {
    workspace_id: workspaceId,
    request_id: requestId,
    path,
  })
}
