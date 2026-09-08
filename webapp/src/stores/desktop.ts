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
    markComputerUseStarted,
    markComputerUseFinished,
    setDisconnected,
    reset,
  }
})
