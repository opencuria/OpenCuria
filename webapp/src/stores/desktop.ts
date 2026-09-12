/**
 * Desktop session store — manages the KasmVNC desktop session state.
 *
 * `isOpen` controls the desktop modal. The session itself (proxy URL,
 * connection state) is independent of the modal: closing the modal keeps
 * the session running so the side-panel live view stays alive.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export const useDesktopStore = defineStore('desktop', () => {
  const isOpen = ref(false)
  const isConnecting = ref(false)
  const isConnected = ref(false)
  const computerUseRuns = ref<Set<string>>(new Set())
  const computerUseActive = computed(() => computerUseRuns.value.size > 0)
  const proxyUrl = ref<string | null>(null)
  const workspaceId = ref<string | null>(null)

  function open(): void {
    isOpen.value = true
  }

  function close(): void {
    isOpen.value = false
  }

  function setConnecting(wsId: string): void {
    workspaceId.value = wsId
    isConnecting.value = true
  }

  function setConnected(wsId: string, url: string): void {
    workspaceId.value = wsId
    proxyUrl.value = url
    isConnected.value = true
    isConnecting.value = false
  }

  function setComputerUseActive(active: boolean): void {
    computerUseRuns.value = active ? new Set(['active']) : new Set()
  }

  /**
   * Drop the viewer lease while keeping the running session mounted.
   *
   * Used when the runner releases the viewer lease but computer-use still
   * holds the desktop process: the iframe stays visible read-only instead
   * of being unmounted. Callers pass the current proxy URL (or null when
   * unknown); an existing mounted URL is never cleared by this path.
   */
  function setViewerReleased(wsId: string, url: string | null): void {
    workspaceId.value = wsId
    if (url) proxyUrl.value = url
    // Viewer lease dropped but the process still runs: keep the mounted
    // iframe connected and read-only instead of tearing down the client.
    isConnected.value = true
    isConnecting.value = false
  }

  function markComputerUseStarted(runId: string): void {
    const next = new Set(computerUseRuns.value)
    next.add(runId)
    computerUseRuns.value = next
  }

  function markComputerUseFinished(runId: string): void {
    const next = new Set(computerUseRuns.value)
    next.delete(runId)
    next.delete('active')
    computerUseRuns.value = next
  }

  function setDisconnected(): void {
    proxyUrl.value = null
    isConnected.value = false
    isConnecting.value = false
  }

  function reset(): void {
    isOpen.value = false
    isConnected.value = false
    isConnecting.value = false
    computerUseRuns.value = new Set()
    proxyUrl.value = null
    workspaceId.value = null
  }

  return {
    isOpen,
    isConnecting,
    isConnected,
    computerUseActive,
    proxyUrl,
    workspaceId,
    open,
    close,
    setConnecting,
    setConnected,
    setComputerUseActive,
    setViewerReleased,
    markComputerUseStarted,
    markComputerUseFinished,
    setDisconnected,
    reset,
  }
})
