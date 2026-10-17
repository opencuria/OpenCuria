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
  const outputSequence = ref(0)
  const pendingInputs = ref<string[]>([])
  const unconfirmedInputs = ref<string[]>([])

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

  function setConnected(id: string, wsId: string, sequence?: number): void {
    terminalId.value = id
    workspaceId.value = wsId
    if (sequence !== undefined && sequence > outputSequence.value) {
      outputSequence.value = sequence
    }
    isConnected.value = true
  }

  function setDisconnected(preserveSession = false): void {
    if (!preserveSession) {
      terminalId.value = null
      outputSequence.value = 0
      pendingInputs.value = []
      unconfirmedInputs.value = []
    }
    isConnected.value = false
  }

  function recordOutput(sequence: number): boolean {
    if (!Number.isSafeInteger(sequence) || sequence <= outputSequence.value) return false
    if (sequence !== outputSequence.value + 1) return false
    outputSequence.value = sequence
    return true
  }

  function addPendingInput(inputId: string): void {
    if (!pendingInputs.value.includes(inputId)) pendingInputs.value.push(inputId)
    if (!unconfirmedInputs.value.includes(inputId)) unconfirmedInputs.value.push(inputId)
  }

  function settlePendingInput(inputId: string): void {
    pendingInputs.value = pendingInputs.value.filter((id) => id !== inputId)
    unconfirmedInputs.value = unconfirmedInputs.value.filter((id) => id !== inputId)
  }

  function confirmPendingInputs(inputIds: string[]): void {
    const confirmed = new Set(inputIds)
    for (const id of confirmed) settlePendingInput(id)
  }

  function reset(): void {
    isOpen.value = false
    isMinimized.value = false
    isConnected.value = false
    terminalId.value = null
    workspaceId.value = null
    outputSequence.value = 0
    pendingInputs.value = []
    unconfirmedInputs.value = []
  }

  return {
    isOpen,
    isMinimized,
    isConnected,
    terminalId,
    workspaceId,
    outputSequence,
    pendingInputs,
    unconfirmedInputs,
    toggle,
    open,
    close,
    minimize,
    restore,
    setConnected,
    setDisconnected,
    recordOutput,
    addPendingInput,
    settlePendingInput,
    confirmPendingInputs,
    reset,
  }
})
