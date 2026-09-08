/**
 * useDesktopSession — shared KasmVNC desktop session logic.
 *
 * Owns start/stop/reconnect, the computer-use take-control flow, VM
 * clipboard sync and the Socket.IO listeners that keep the desktop store
 * in sync. Socket listeners are set up once by DesktopSurface; the side
 * panel and the desktop modal only use the actions.
 */

import { ref, type Ref } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useNotificationStore } from '@/stores/notifications'
import * as workspacesApi from '@/services/workspaces.api'
import { onEvent } from '@/services/socket'

export function useDesktopSession(workspaceId: Ref<string>) {
  const desktopStore = useDesktopStore()
  const notifications = useNotificationStore()
  const error = ref<string | null>(null)
  const takeControlBusy = ref(false)
  const clipboardBusy = ref(false)
  const cleanupFns: (() => void)[] = []

  async function startDesktop(): Promise<void> {
    if (desktopStore.workspaceId && desktopStore.workspaceId !== workspaceId.value) {
      desktopStore.reset()
    }
    if (desktopStore.isConnecting || desktopStore.isConnected) return
    error.value = null
    desktopStore.setConnecting(workspaceId.value)

    try {
      const status = await workspacesApi.getDesktopStatus(workspaceId.value)
      desktopStore.setComputerUseActive(Boolean(status.computer_use_active))
      await workspacesApi.startDesktop(workspaceId.value)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      if (msg.includes('409') || msg.toLowerCase().includes('conflict')) {
        try {
          const status = await workspacesApi.getDesktopStatus(workspaceId.value)
          desktopStore.setComputerUseActive(Boolean(status.computer_use_active))
          if (status.active && status.proxy_url) {
            desktopStore.setConnected(workspaceId.value, status.proxy_url)
            return
          }
        } catch {
          // fall through
        }
      }
      error.value = msg
      desktopStore.setDisconnected()
    }
  }

  async function stopDesktop(): Promise<boolean> {
    try {
      await workspacesApi.stopDesktop(workspaceId.value)
      desktopStore.setDisconnected()
      return true
    } catch {
      error.value = 'Failed to stop desktop session'
      return false
    }
  }

  async function stopDesktopIfActive(targetWorkspaceId: string): Promise<void> {
    if (desktopStore.workspaceId !== targetWorkspaceId) return
    if (!desktopStore.isConnected && !desktopStore.isConnecting) return
    try {
      await workspacesApi.stopDesktop(targetWorkspaceId)
    } catch {
      // Ignore stop errors during teardown.
    }
    desktopStore.setDisconnected()
  }

  function handleReconnect(): void {
    desktopStore.setDisconnected()
    void startDesktop()
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
      await navigator.clipboard.writeText(text || '')
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
    cleanupFns.push(
      onEvent('desktop:started', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        desktopStore.setConnected(workspaceId.value, data.proxy_url)
        if (data.computer_use_active) desktopStore.setComputerUseActive(true)
      }),
      onEvent('desktop:stopped', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        desktopStore.setDisconnected()
        desktopStore.setComputerUseActive(false)
      }),
      onEvent('desktop:viewer_released', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        desktopStore.setDisconnected()
        desktopStore.setComputerUseActive(Boolean(data.computer_use_active))
      }),
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
      onEvent('workspace:error', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        if (desktopStore.isConnecting) {
          error.value = data.error
          desktopStore.setDisconnected()
        }
      }),
    )
  }

  function cleanupSocketListeners(): void {
    cleanupFns.forEach((fn) => fn())
    cleanupFns.length = 0
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
