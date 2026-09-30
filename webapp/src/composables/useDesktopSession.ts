/**
 * useDesktopSession — shared KasmVNC desktop session logic.
 *
 * Owns start/stop/reconnect, the computer-use take-control flow, VM
 * clipboard sync and the Socket.IO listeners that keep the desktop store
 * in sync. Socket listeners are set up once by DesktopSurface; the side
 * panel and the desktop modal only use the actions.
 */

import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useNotificationStore } from '@/stores/notifications'
import * as workspacesApi from '@/services/workspaces.api'
import { writeClipboardText } from '@/lib/clipboard'
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

interface PendingDesktopStart {
  promise: Promise<void>
  phase: 'status' | 'post'
  cancelled: boolean
  owners: Set<object>
}

// Component trees can mount more than one desktop surface for the same
// workspace. These requests coordinate their shared runner lease.
const pendingDesktopStarts = new Map<string, PendingDesktopStart>()
const desktopStopRequests = new Map<string, Promise<void>>()
const knownDesktopLeases = new Set<string>()

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
  let startGeneration = 0
  const owner = {}
  // Remember sessions even if a parent resets Pinia before teardown runs.
  const knownLeaseWorkspaceIds = new Set<string>()
  if (
    desktopStore.workspaceId === workspaceId.value &&
    (desktopStore.isConnected || desktopStore.isConnecting)
  ) {
    knownLeaseWorkspaceIds.add(workspaceId.value)
    knownDesktopLeases.add(workspaceId.value)
  }

  function cancelOwnedStart(targetWorkspaceId?: string): void {
    for (const [id, pending] of pendingDesktopStarts) {
      if (targetWorkspaceId !== undefined && id !== targetWorkspaceId) continue
      if (!pending.owners.has(owner)) continue
      pending.owners.delete(owner)
      if (pending.owners.size === 0) {
        pending.cancelled = true
        if (pending.phase === 'status' && pendingDesktopStarts.get(id) === pending) {
          pendingDesktopStarts.delete(id)
        }
      }
    }
  }

  function releaseOrphanedPosts(): void {
    for (const [id, pending] of pendingDesktopStarts) {
      if (pending.owners.size > 0 || pending.phase !== 'post') continue
      pending.cancelled = true
      knownDesktopLeases.delete(id)
      knownLeaseWorkspaceIds.delete(id)
      if (desktopStore.workspaceId === id) {
        desktopStore.setDisconnected()
        desktopStore.setComputerUseActive(false)
      }
      void requestStopDesktop(id).catch(() => undefined)
    }
  }

  function disposeScope(): void {
    runGeneration += 1
    startGeneration += 1
    cancelOwnedStart()
    releaseOrphanedPosts()
    cleanupFns.forEach((fn) => fn())
    cleanupFns.length = 0
  }

  if (getCurrentScope()) onScopeDispose(disposeScope)

  // Invalidate a start even if the workspace changes away and back before
  // its pending API request settles. A POST already sent cannot be cancelled;
  // the global stop coordinator will wait for it and release the lease.
  watch(workspaceId, (_current, previous) => {
    startGeneration += 1
    cancelOwnedStart(previous)
    releaseOrphanedPosts()
  }, { flush: 'sync' })

  function startDesktop(): Promise<void> {
    const currentWorkspace = workspaceId.value
    const cancelledStart = pendingDesktopStarts.get(currentWorkspace)
    if (cancelledStart?.cancelled) {
      return cancelledStart.promise.then(() => {
        if (workspaceId.value !== currentWorkspace) return
        return startDesktop()
      })
    }

    const stopInProgress = desktopStopRequests.get(currentWorkspace)
    if (stopInProgress) {
      return stopInProgress.then(() => {
        if (workspaceId.value !== currentWorkspace) return
        return startDesktop()
      })
    }

    const existingStart = pendingDesktopStarts.get(currentWorkspace)
    if (existingStart && !existingStart.cancelled) {
      existingStart.owners.add(owner)
      return existingStart.promise
    }

    if (desktopStore.workspaceId && desktopStore.workspaceId !== currentWorkspace) {
      desktopStore.reset()
    }
    if (desktopStore.isConnecting || desktopStore.isConnected) return Promise.resolve()

    startGeneration += 1
    const pending: PendingDesktopStart = {
      // Replaced below immediately after registering the request globally.
      promise: Promise.resolve(),
      phase: 'status',
      cancelled: false,
      owners: new Set([owner]),
    }
    const isCurrentStart = (): boolean =>
      !pending.cancelled &&
      pendingDesktopStarts.get(currentWorkspace) === pending &&
      currentWorkspace === workspaceId.value &&
      desktopStore.workspaceId === currentWorkspace

    error.value = null
    knownDesktopLeases.add(currentWorkspace)
    knownLeaseWorkspaceIds.add(currentWorkspace)
    desktopStore.setConnecting(currentWorkspace)

    // Register synchronously before the first await, so a second surface's
    // teardown can cancel a delayed status request or wait for an in-flight POST.
    pendingDesktopStarts.set(currentWorkspace, pending)
    const request = (async () => {
      try {
        const status = await workspacesApi.getDesktopStatus(currentWorkspace)
        if (!isCurrentStart()) return
        desktopStore.setComputerUseActive(Boolean(status.computer_use_active))
        // Set phase before invoking the POST: a concurrent teardown must wait
        // for this request before sending its stop.
        pending.phase = 'post'
        await workspacesApi.startDesktop(currentWorkspace)
        if (pending.cancelled) {
          knownDesktopLeases.delete(currentWorkspace)
          knownLeaseWorkspaceIds.delete(currentWorkspace)
        }
      } catch (err: unknown) {
        if (!isCurrentStart()) return
        const msg = err instanceof Error ? err.message : String(err)
        if (msg.includes('409') || msg.toLowerCase().includes('conflict')) {
          try {
            const status = await workspacesApi.getDesktopStatus(currentWorkspace)
            if (!isCurrentStart()) return
            desktopStore.setComputerUseActive(Boolean(status.computer_use_active))
            if (status.active && status.proxy_url) {
              desktopStore.setConnected(currentWorkspace, status.proxy_url)
              return
            }
          } catch {
            // fall through
          }
        }
        if (!isCurrentStart()) return
        error.value = msg
        desktopStore.setDisconnected()
        knownDesktopLeases.delete(currentWorkspace)
        knownLeaseWorkspaceIds.delete(currentWorkspace)
      } finally {
        if (pendingDesktopStarts.get(currentWorkspace) === pending) {
          pendingDesktopStarts.delete(currentWorkspace)
        }
      }
    })()
    pending.promise = request
    return request
  }

  function requestStopDesktop(targetWorkspaceId: string): Promise<void> {
    const existing = desktopStopRequests.get(targetWorkspaceId)
    if (existing) return existing

    const pending = pendingDesktopStarts.get(targetWorkspaceId)
    // A status GET has not acquired a lease yet; cancel it and release now.
    // If POST was already sent, wait for it before releasing so it cannot
    // recreate a lease after the stop request.
    if (pending?.phase === 'status') {
      pending.cancelled = true
      if (pendingDesktopStarts.get(targetWorkspaceId) === pending) {
        pendingDesktopStarts.delete(targetWorkspaceId)
      }
    }

    let resolveRequest!: () => void
    let rejectRequest!: (reason?: unknown) => void
    const request = new Promise<void>((resolve, reject) => {
      resolveRequest = resolve
      rejectRequest = reject
    })
    // Teardown callers intentionally ignore failures; attach a handler here
    // while returning the original rejecting promise to explicit stop callers.
    void request.catch(() => undefined)
    desktopStopRequests.set(targetWorkspaceId, request)
    const issueStop = () => {
      try {
        void workspacesApi.stopDesktop(targetWorkspaceId).then(
          () => resolveRequest(),
          (error: unknown) => rejectRequest(error),
        )
      } catch (error: unknown) {
        rejectRequest(error)
      }
    }
    if (pending?.phase === 'post') {
      void pending.promise.then(issueStop, issueStop)
    } else {
      issueStop()
    }
    const clearRequest = () => {
      if (desktopStopRequests.get(targetWorkspaceId) === request) {
        desktopStopRequests.delete(targetWorkspaceId)
      }
    }
    void request.then(clearRequest, clearRequest)
    return request
  }

  async function stopDesktop(): Promise<boolean> {
    const currentWorkspace = workspaceId.value
    const generation = runGeneration
    const ownsStoreAtStart = desktopStore.workspaceId === currentWorkspace
    const isStale = (): boolean =>
      generation !== runGeneration ||
      currentWorkspace !== workspaceId.value ||
      (ownsStoreAtStart && desktopStore.workspaceId !== currentWorkspace)
    try {
      await requestStopDesktop(currentWorkspace)
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
      if (!isStale()) error.value = 'Failed to stop desktop session'
      return false
    }
  }

  function stopDesktopIfActive(targetWorkspaceId: string): Promise<void> {
    const ownsStore = desktopStore.workspaceId === targetWorkspaceId
    const hasStoreSession = ownsStore && (desktopStore.isConnected || desktopStore.isConnecting)
    const pendingStart = pendingDesktopStarts.get(targetWorkspaceId)
    const existingStop = desktopStopRequests.get(targetWorkspaceId)
    if (existingStop) {
      if (workspaceId.value === targetWorkspaceId) startGeneration += 1
      cancelOwnedStart(targetWorkspaceId)
      if (ownsStore) {
        desktopStore.setDisconnected()
        desktopStore.setComputerUseActive(false)
      }
      return existingStop.catch(() => undefined)
    }
    if (
      !hasStoreSession &&
      !knownLeaseWorkspaceIds.has(targetWorkspaceId) &&
      !knownDesktopLeases.has(targetWorkspaceId) &&
      !pendingStart
    ) return Promise.resolve()

    // A teardown also invalidates pending starts for this workspace. The
    // captured id ensures an old async completion can never become a start
    // for the next workspace.
    if (workspaceId.value === targetWorkspaceId) startGeneration += 1
    cancelOwnedStart(targetWorkspaceId)
    knownLeaseWorkspaceIds.delete(targetWorkspaceId)
    knownDesktopLeases.delete(targetWorkspaceId)

    // Local disconnect first, but only mutate the store if it still belongs
    // to the lease being released. Parent resets may already have initialized
    // the shared store for another workspace.
    if (ownsStore) {
      desktopStore.setDisconnected()
      desktopStore.setComputerUseActive(false)
    }

    // Start the release without holding the component's workspace watcher
    // open. The caller can reset its store immediately after local teardown,
    // before a new workspace can start; the runner request remains deduped
    // and settles independently.
    return requestStopDesktop(targetWorkspaceId).catch(() => {
      // Ignore stop errors during teardown.
    })
  }

  function handleReconnect(): void {
    // Keep the runner session; remount the iframe so a dropped KasmVNC
    // client cannot keep its idle timer against a torn-down UI.rfb.
    if (desktopStore.isConnected) {
      desktopStore.bumpViewer()
      return
    }
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
    const isCurrentEvent = (data: { workspace_id?: string }): boolean =>
      data.workspace_id === workspaceId.value &&
      (desktopStore.workspaceId === null || desktopStore.workspaceId === data.workspace_id)

    const ownsCurrentWorkspace = (): boolean =>
      desktopStore.workspaceId === null || desktopStore.workspaceId === workspaceId.value

    cleanupFns.push(
      onEvent('desktop:started', (data) => {
        if (!isCurrentEvent(data)) return
        knownDesktopLeases.add(data.workspace_id)
        knownLeaseWorkspaceIds.add(data.workspace_id)
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
        if (!isCurrentEvent(data)) return
        knownDesktopLeases.delete(data.workspace_id)
        knownLeaseWorkspaceIds.delete(data.workspace_id)
        desktopStore.setDisconnected()
        desktopStore.setComputerUseActive(false)
      }),
      onEvent('desktop:viewer_released', (data) => {
        if (!isCurrentEvent(data)) return
        knownDesktopLeases.add(data.workspace_id)
        knownLeaseWorkspaceIds.add(data.workspace_id)
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
        if (!data.computer_use_active) {
          knownDesktopLeases.delete(data.workspace_id)
          knownLeaseWorkspaceIds.delete(data.workspace_id)
        }
      }),
      onEvent('harness.subtask_started', (data) => {
        if (data.workspace_id !== workspaceId.value || !ownsCurrentWorkspace()) return
        if ((data.agent || '').toLowerCase() !== 'computeruse') return
        desktopStore.markComputerUseStarted(data.child_session_id || data.subtask_id)
      }),
      onEvent('harness.subtask_finished', (data) => {
        if (data.workspace_id !== workspaceId.value || !ownsCurrentWorkspace()) return
        if ((data.agent || '').toLowerCase() !== 'computeruse') return
        desktopStore.markComputerUseFinished(data.child_session_id || data.subtask_id)
      }),
      onEvent('workspace:error', (data) => {
        if (data.workspace_id !== workspaceId.value || !ownsCurrentWorkspace()) return
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
    // workspace identity check as well). This explicit method is also used
    // by existing lifecycle hooks; scope disposal covers all other callers.
    disposeScope()
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
