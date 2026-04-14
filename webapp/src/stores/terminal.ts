/**
 * Terminal store — manages the interactive terminal panel state.
 *
 * Tracks whether the terminal is open/connected and stores the
 * terminal_id received from the backend after PTY creation.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useTerminalStore = defineStore('terminal', () => {
  const isOpen = ref(false)
  const isMinimized = ref(false)
  const isConnected = ref(false)
  const terminalId = ref<string | null>(null)
  const workspaceId = ref<string | null>(null)

  function toggle(): void {
    isOpen.value = !isOpen.value
  }

  function open(): void {
    isOpen.value = true
    isMinimized.value = false
  }

  function close(): void {
    isOpen.value = false
    isMinimized.value = false
  }

  function minimize(): void {
    if (!isOpen.value) return
    isMinimized.value = true
  }

  function restore(): void {
    isOpen.value = true
    isMinimized.value = false
  }

  function setConnected(id: string, wsId: string): void {
    terminalId.value = id
    workspaceId.value = wsId
    isConnected.value = true
  }

  function setDisconnected(): void {
    terminalId.value = null
    isConnected.value = false
  }

  function reset(): void {
    isOpen.value = false
    isMinimized.value = false
    isConnected.value = false
    terminalId.value = null
    workspaceId.value = null
  }

  return {
    isOpen,
    isMinimized,
    isConnected,
    terminalId,
    workspaceId,
    toggle,
    open,
    close,
    minimize,
    restore,
    setConnected,
    setDisconnected,
    reset,
  }
})
