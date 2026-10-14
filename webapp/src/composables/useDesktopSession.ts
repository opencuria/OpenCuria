/** Shared desktop actions. Only locally retained surfaces own viewer intents. */
import { computed, getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useNotificationStore } from '@/stores/notifications'
import * as workspacesApi from '@/services/workspaces.api'
import { copyToClipboard } from '@/lib/clipboard'
import { onEvent } from '@/services/socket'
import {
  acquireDesktopViewer,
  closeDesktopViewer,
  desktopViewerSession,
  refreshDesktopViewer,
  releaseDesktopViewer,
  retainDesktopViewer,
} from './useDesktopSessionCoordinator'

export const STOP_DESKTOP_POLL_TIMEOUT_MS = 5000
export const STOP_DESKTOP_POLL_INTERVAL_MS = 250
export interface DesktopSessionOptions {
  observer?: boolean
  sleep?: (ms: number) => Promise<void>
  stopPollTimeoutMs?: number
  stopPollIntervalMs?: number
}

export function useDesktopSession(workspaceId: Ref<string>, options?: DesktopSessionOptions) {
  const desktopStore = useDesktopStore()
  const notifications = useNotificationStore()
  const session = computed(() => desktopViewerSession(workspaceId.value))
  const error = ref<string | null>(null)
  const takeControlBusy = ref(false)
  const clipboardBusy = ref(false)
  const owner = {}
  const retained = new Set<string>()
  const cleanupFns: (() => void)[] = []
  let disposed = false
  let reconnectPending: Promise<void> | null = null
  let workspaceGeneration = 0
  const vmClipboardControllers = new Set<AbortController>()

  function retain(id: string): void {
    if (options?.observer || disposed || retained.has(id)) return
    retained.add(id)
    retainDesktopViewer(id, owner)
  }
  // Refcount surfaces, not starts: a later mount protects an existing intent.
  retain(workspaceId.value)
  if (desktopStore.workspaceId && desktopStore.workspaceId !== workspaceId.value)
    desktopStore.setDisconnected()
  const stopStateWatch = watch(
    session,
    (state) => {
      if (disposed) return
      const id = workspaceId.value
      if (
        desktopStore.workspaceId &&
        desktopStore.workspaceId !== id &&
        (desktopStore.isConnected || desktopStore.isConnecting)
      )
        return
      error.value = state.error
      const startupFailed = Boolean(state.startupDeadline && !state.connecting && state.error)
      if (startupFailed && desktopStore.workspaceId === id) {
        // A failure wins over an older active snapshot retained in the session.
        desktopStore.setDisconnected()
      } else if (state.status) {
        desktopStore.setComputerUseActive(Boolean(state.status.computer_use_active))
        desktopStore.setGlobalHolders(
          Boolean(state.status.mcp_active),
          state.status.holder_count ?? 0,
        )
        if (state.status.active && state.status.proxy_url) {
          if (state.connecting) desktopStore.setConnecting(id)
          else desktopStore.setConnected(id, state.status.proxy_url)
        } else if (desktopStore.workspaceId === id) {
          // A stale route/status must not preserve an old connected store flag.
          desktopStore.setDisconnected()
        }
      }
      desktopStore.setViewerIntent(
        state.wanted ? state.revision : null,
        state.leaseState,
        state.epoch,
      )
      if (state.connecting) desktopStore.setConnecting(id)
    },
    { deep: true, immediate: true, flush: 'sync' },
  )

  async function stopDesktopIfActive(id: string): Promise<void> {
    if (!retained.delete(id)) return
    await releaseDesktopViewer(id, owner)
  }
  const stopWorkspaceWatch = watch(
    workspaceId,
    (id, previous) => {
      workspaceGeneration += 1
      for (const controller of vmClipboardControllers) controller.abort()
      vmClipboardControllers.clear()
      void stopDesktopIfActive(previous)
      // Previous workspace connection flags must not suppress the new
      // sidebar's auto-start. This is routing state, not inherited ownership.
      if (desktopStore.workspaceId === previous) desktopStore.setDisconnected()
      retain(id)
    },
    { flush: 'sync' },
  )

  function cleanupSocketListeners(): void {
    if (disposed) return
    disposed = true
    for (const controller of vmClipboardControllers) controller.abort()
    vmClipboardControllers.clear()
    stopStateWatch()
    stopWorkspaceWatch()
    cleanupFns.splice(0).forEach((fn) => fn())
    for (const id of retained) void stopDesktopIfActive(id)
  }
  if (getCurrentScope()) onScopeDispose(cleanupSocketListeners)

  function startDesktop(): Promise<void> {
    if (options?.observer || disposed) return Promise.resolve()
    retain(workspaceId.value)
    return acquireDesktopViewer(workspaceId.value)
  }
  function stopDesktop(): Promise<boolean> {
    if (options?.observer) return Promise.resolve(true)
    return closeDesktopViewer(workspaceId.value)
  }
  function handleReconnect(): Promise<void> {
    if (reconnectPending) return reconnectPending
    const id = workspaceId.value
    reconnectPending = (async () => {
      if (!desktopStore.isConnected || desktopStore.workspaceId !== id) {
        await startDesktop()
        return
      }

      const status = await refreshDesktopViewer(id, true)
      if (disposed || workspaceId.value !== id || !status?.active || !status.proxy_url) return
      desktopStore.bumpViewer()
    })().finally(() => {
      reconnectPending = null
    })
    return reconnectPending
  }

  async function takeControl(): Promise<void> {
    if (takeControlBusy.value) return
    takeControlBusy.value = true
    desktopStore.setComputerUseActive(false)
    try {
      await workspacesApi.takeDesktopControl(workspaceId.value)
    } catch (err: unknown) {
      desktopStore.setComputerUseActive(true)
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      takeControlBusy.value = false
    }
  }

  async function copyFromVmClipboard(options?: {
    delayMs?: number
    context?: Window
    fallback?: 'dialog' | 'none'
    loadText?: () => Promise<string>
  }): Promise<boolean> {
    const requestedWorkspaceId = workspaceId.value
    const requestedGeneration = workspaceGeneration
    if (
      !desktopStore.isConnected ||
      desktopStore.workspaceId !== requestedWorkspaceId ||
      desktopStore.computerUseActive ||
      clipboardBusy.value ||
      disposed
    )
      return false

    clipboardBusy.value = true
    const controller = new AbortController()
    vmClipboardControllers.add(controller)
    const signal = controller.signal
    const isCurrentOperation = (): boolean =>
      !signal.aborted &&
      !disposed &&
      workspaceGeneration === requestedGeneration &&
      workspaceId.value === requestedWorkspaceId &&
      desktopStore.workspaceId === requestedWorkspaceId
    const loadText = async (): Promise<string> => {
      if (options?.delayMs && options.delayMs > 0)
        await new Promise<void>((resolve) => window.setTimeout(resolve, options.delayMs))
      if (!isCurrentOperation() || !desktopStore.isConnected || desktopStore.computerUseActive)
        throw new Error('VM clipboard request is no longer current')

      let text: string
      try {
        if (options?.loadText) text = await options.loadText()
        else {
          const response = await workspacesApi.readDesktopClipboard(requestedWorkspaceId)
          text = response.text
        }
      } catch {
        // Do not expose API/network details through the generic clipboard error.
        throw new Error('Unable to load VM clipboard')
      }
      if (!isCurrentOperation() || !desktopStore.isConnected || desktopStore.computerUseActive)
        throw new Error('VM clipboard request is no longer current')
      return text || ''
    }

    let removeAbortListener: (() => void) | undefined
    try {
      const copyOptions = {
        success: 'silent' as const,
        signal,
        ...(options?.context !== undefined ? { context: options.context } : {}),
        ...(options?.fallback !== undefined ? { fallback: options.fallback } : {}),
      }
      const copyRequest = copyToClipboard(loadText, 'VM clipboard', copyOptions)
      const copied = await Promise.race([
        copyRequest,
        new Promise<boolean>((resolve) => {
          const onAbort = (): void => resolve(false)
          signal.addEventListener('abort', onAbort, { once: true })
          removeAbortListener = () => signal.removeEventListener('abort', onAbort)
          if (signal.aborted) onAbort()
        }),
      ])
      if (!copied || !isCurrentOperation()) return false
      if (options?.fallback !== 'none') notifications.success('Copied from VM')
      return true
    } finally {
      removeAbortListener?.()
      vmClipboardControllers.delete(controller)
      clipboardBusy.value = false
    }
  }

  async function pasteToVmClipboard(plainText?: string): Promise<boolean> {
    const requestedWorkspaceId = workspaceId.value
    const requestedGeneration = workspaceGeneration
    const isCurrentOperation = (): boolean =>
      !disposed &&
      workspaceGeneration === requestedGeneration &&
      workspaceId.value === requestedWorkspaceId &&
      desktopStore.isConnected &&
      desktopStore.workspaceId === requestedWorkspaceId &&
      !desktopStore.computerUseActive

    if (!isCurrentOperation() || clipboardBusy.value) return false
    clipboardBusy.value = true
    let readingBrowserClipboard = false
    try {
      if (!isCurrentOperation()) return false
      let text = plainText
      if (text === undefined) {
        readingBrowserClipboard = true
        if (
          typeof navigator === 'undefined' ||
          typeof navigator.clipboard?.readText !== 'function'
        ) {
          if (isCurrentOperation())
            notifications.error('Paste failed', 'Use Cmd/Ctrl+V inside the desktop to paste.')
          return false
        }
        text = await navigator.clipboard.readText()
        readingBrowserClipboard = false
        if (!isCurrentOperation()) return false
      }

      if (!isCurrentOperation()) return false
      await workspacesApi.writeDesktopClipboard(requestedWorkspaceId, text)
      if (!isCurrentOperation()) return false
      // Native paste events carry trusted text and must not produce another toast.
      if (plainText === undefined)
        notifications.success('Pasted to VM', 'Local clipboard sent to VM clipboard.')
      return true
    } catch {
      if (isCurrentOperation()) {
        notifications.error(
          'Paste failed',
          readingBrowserClipboard
            ? 'Use Cmd/Ctrl+V inside the desktop to paste.'
            : 'Unable to write to the VM clipboard. Please try again.',
        )
      }
      return false
    } finally {
      clipboardBusy.value = false
    }
  }

  function setupSocketListeners(): void {
    if (disposed || cleanupFns.length) return
    // Broadcast summaries never convey this tab's membership.
    for (const event of [
      'desktop:started',
      'desktop:stopped',
      'desktop:viewer_released',
    ] as const) {
      cleanupFns.push(
        onEvent(event, (data) => {
          if (data.workspace_id !== workspaceId.value) return
          void refreshDesktopViewer(workspaceId.value)
        }),
      )
    }
    cleanupFns.push(
      onEvent('harness.subtask_started', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        if ((data.agent || '').toLowerCase() !== 'computeruse') return
        desktopStore.markComputerUseStarted(data.child_session_id || data.subtask_id)
      }),
      onEvent('harness.subtask_finished', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        if ((data.agent || '').toLowerCase() !== 'computeruse') return
        desktopStore.markComputerUseFinished(data.child_session_id || data.subtask_id)
      }),
    )
  }
  return {
    error,
    takeControlBusy,
    clipboardBusy,
    startDesktop,
    stopDesktop,
    stopDesktopIfActive,
    handleReconnect,
    takeControl,
    copyFromVmClipboard,
    pasteToVmClipboard,
    setupSocketListeners,
    cleanupSocketListeners,
  }
}
