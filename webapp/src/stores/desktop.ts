/**
 * Desktop session store — manages the KasmVNC desktop panel state.
 *
 * Tracks whether the desktop viewer is open, whether a session is
 * active, and stores the proxy URL for the KasmVNC iframe.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useDesktopStore = defineStore('desktop', () => {
  const isOpen = ref(false)
  const isConnecting = ref(false)
  const isConnected = ref(false)
  const proxyUrl = ref<string | null>(null)
  const workspaceId = ref<string | null>(null)

  function toggle(): void {
    isOpen.value = !isOpen.value
  }

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

  function setDisconnected(): void {
    proxyUrl.value = null
    isConnected.value = false
    isConnecting.value = false
  }

  function reset(): void {
    isOpen.value = false
    isConnected.value = false
    isConnecting.value = false
    proxyUrl.value = null
    workspaceId.value = null
  }

  return {
    isOpen,
    isConnecting,
    isConnected,
    proxyUrl,
    workspaceId,
    toggle,
    open,
    close,
    setConnecting,
    setConnected,
    setDisconnected,
    reset,
  }
})
