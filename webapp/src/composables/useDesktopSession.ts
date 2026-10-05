/** Shared desktop actions. Only locally retained surfaces own viewer intents. */
import { computed, getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useNotificationStore } from '@/stores/notifications'
import * as workspacesApi from '@/services/workspaces.api'
import { writeClipboardText } from '@/lib/clipboard'
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
        desktopStore.workspaceId && desktopStore.workspaceId !== id
        && (desktopStore.isConnected || desktopStore.isConnecting)
      ) return
      error.value = state.error
      if (state.status) {
        desktopStore.setComputerUseActive(Boolean(state.status.computer_use_active))
        desktopStore.setGlobalHolders(
          Boolean(state.status.mcp_active),
          state.status.holder_count ?? 0,
        )
        if (state.status.active && state.status.proxy_url)
          desktopStore.setConnected(id, state.status.proxy_url)
        else if (!state.connecting) desktopStore.setDisconnected()
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
  function handleReconnect(): void {
    if (desktopStore.isConnected) {
      desktopStore.bumpViewer()
      void refreshDesktopViewer(workspaceId.value)
    } else void startDesktop()
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

  async function copyFromVmClipboard(): Promise<boolean> {
    if (!desktopStore.isConnected || clipboardBusy.value) return false
    clipboardBusy.value = true
    try {
      const { text } = await workspacesApi.readDesktopClipboard(workspaceId.value)
      const ok = await writeClipboardText(text || '')
      if (!ok) {
        notifications.error('Copy failed', 'Clipboard unavailable')
        return false
      }
      notifications.success('Copied from VM', 'VM clipboard copied to local clipboard.')
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      notifications.error('Copy failed', msg)
      return false
    } finally {
      clipboardBusy.value = false
    }
  }

  async function pasteToVmClipboard(): Promise<boolean> {
    if (!desktopStore.isConnected || clipboardBusy.value) return false
    clipboardBusy.value = true
    try {
      const text = await navigator.clipboard.readText()
      await workspacesApi.writeDesktopClipboard(workspaceId.value, text || '')
      notifications.success('Pasted to VM', 'Local clipboard sent to VM clipboard.')
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      notifications.error('Paste failed', msg)
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
