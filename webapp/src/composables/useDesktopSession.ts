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

export const STOP_DESKTOP_POLL_TIMEOUT_MS = 5000
export const STOP_DESKTOP_POLL_INTERVAL_MS = 250

type DesktopStatus = Awaited<ReturnType<typeof workspacesApi.getDesktopStatus>>

export interface DesktopSessionOptions {
  sleep?: (ms: number) => Promise<void>
  stopPollTimeoutMs?: number
  stopPollIntervalMs?: number
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export function useDesktopSession(workspaceId: Ref<string>, options?: DesktopSessionOptions) {
  const desktopStore = useDesktopStore()
  const notifications = useNotificationStore()
  const error = ref<string | null>(null)
  const takeControlBusy = ref(false)
  const clipboardBusy = ref(false)
  const cleanupFns: (() => void)[] = []
  // Run generation: bumped by cleanupSocketListeners so stale poll loops
  // (parallel stopDesktop calls, unmounted composables, workspace
  // switches) can never mutate the store after they were invalidated.
  // No AbortController needed: the bounded poll only checks this token
  // after each awaited boundary.
  let runGeneration = 0

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
    const currentWorkspace = workspaceId.value
    const generation = runGeneration
    const isStale = (): boolean =>
      generation !== runGeneration || currentWorkspace !== workspaceId.value
    try {
      await workspacesApi.stopDesktop(currentWorkspace)
      if (isStale()) return true
      // The runner decides asynchronously whether computer-use still holds
      // the process, so the status right after the POST is usually stale
      // (viewer_held still true). The socket event stays authoritative and
      // may update the store during the poll; poll the status until the
      // session is gone (!active) or the viewer lease is released
      // (viewer_held false), bounded to 5s with short intervals.
      const sleep = options?.sleep ?? defaultSleep
      const timeoutMs = options?.stopPollTimeoutMs ?? STOP_DESKTOP_POLL_TIMEOUT_MS
      const intervalMs = options?.stopPollIntervalMs ?? STOP_DESKTOP_POLL_INTERVAL_MS
      const deadline = Date.now() + timeoutMs
      const maxAttempts = Math.max(1, Math.ceil(timeoutMs / Math.max(intervalMs, 1)) + 1)
      let consecutiveErrors = 0
      let attempts = 0
      for (;;) {
        attempts += 1
        let status: DesktopStatus | null = null
        try {
          status = await workspacesApi.getDesktopStatus(currentWorkspace)
          consecutiveErrors = 0
        } catch {
          consecutiveErrors += 1
          // The poll itself keeps failing: if computer-use is known to
          // hold the process, keep the iframe mounted read-only until a
          // socket event arrives. Otherwise disconnect locally so no dead
          // KasmVNC client stays mounted on a stopped desktop.
          if (consecutiveErrors >= 3 || Date.now() >= deadline || attempts >= maxAttempts) {
            if (isStale()) return true
            if (!desktopStore.computerUseActive) {
              desktopStore.setDisconnected()
            }
            return true
          }
          await sleep(intervalMs)
          if (isStale()) return true
          continue
        }
        if (isStale()) return true
        // Socket events stay primary: the store may already reflect the
        // final state (or a newer workspace action) while polling.
        if (!status.active || status.viewer_held === false) {
          if (
            desktopStore.workspaceId !== null &&
            desktopStore.workspaceId !== currentWorkspace
          ) {
            // Workspace switched mid-poll: never overwrite the new
            // workspace state with the old workspace status.
            return true
          }
          if (!status.active) {
            desktopStore.setDisconnected()
            desktopStore.setComputerUseActive(false)
          } else {
            desktopStore.setComputerUseActive(Boolean(status.computer_use_active))
            if (status.proxy_url) {
              desktopStore.setViewerReleased(currentWorkspace, status.proxy_url)
            } else {
              desktopStore.setDisconnected()
              desktopStore.setComputerUseActive(false)
            }
          }
          return true
        }
        if (
          desktopStore.workspaceId !== currentWorkspace ||
          (!desktopStore.isConnected && !desktopStore.isConnecting)
        ) {
          // A socket event already settled the stop (or the session was
          // torn down elsewhere): stop polling instead of overwriting it.
          return true
        }
        if (Date.now() >= deadline || attempts >= maxAttempts) {
          // Timeout with a stale viewer lease: keep the session mounted
          // read-only only when computer-use is known to hold the
          // process; otherwise disconnect locally so no dead KasmVNC
          // client stays mounted.
          if (isStale()) return true
          if (!desktopStore.computerUseActive) {
            desktopStore.setDisconnected()
          }
          return true
        }
        await sleep(intervalMs)
        if (isStale()) return true
      }
    } catch {
      error.value = 'Failed to stop desktop session'
      return false
    }
  }

  async function stopDesktopIfActive(targetWorkspaceId: string): Promise<void> {
    if (desktopStore.workspaceId !== targetWorkspaceId) return
    if (!desktopStore.isConnected && !desktopStore.isConnecting) return
    // Local disconnect first: teardown must never leave a stale mounted
    // iframe behind when the stop POST hangs or the socket event is lost.
    desktopStore.setDisconnected()
    desktopStore.setComputerUseActive(false)
    try {
      await workspacesApi.stopDesktop(targetWorkspaceId)
    } catch {
      // Ignore stop errors during teardown.
    }
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
        // Same proxy URL: keep the existing iframe instead of remounting
        // the KasmVNC client (avoids UI.rfb churn on duplicate events).
        if (
          desktopStore.isConnected &&
          desktopStore.proxyUrl === data.proxy_url
        ) {
          desktopStore.setComputerUseActive(Boolean(data.computer_use_active))
          return
        }
        desktopStore.setConnected(workspaceId.value, data.proxy_url)
        desktopStore.setComputerUseActive(Boolean(data.computer_use_active))
      }),
      onEvent('desktop:stopped', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        desktopStore.setDisconnected()
        desktopStore.setComputerUseActive(false)
      }),
      onEvent('desktop:viewer_released', (data) => {
        if (data.workspace_id !== workspaceId.value) return
        // The viewer lease is gone but computer-use still holds the Xvnc
        // process: keep the iframe mounted read-only instead of tearing
        // down the KasmVNC client mid-session.
        if (data.computer_use_active) {
          desktopStore.setViewerReleased(
            workspaceId.value,
            desktopStore.proxyUrl,
          )
          desktopStore.setComputerUseActive(true)
          return
        }
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
    // Invalidate in-flight stopDesktop poll loops: their next awaited
    // boundary observes the bumped generation and returns without
    // touching the store (covers parallel stopDesktop calls and
    // unmounted composables; workspace switches are covered by the
    // workspace identity check as well).
    runGeneration += 1
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
