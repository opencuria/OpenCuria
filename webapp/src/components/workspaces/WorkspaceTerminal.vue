<script setup lang="ts">
/**
 * WorkspaceTerminal — interactive PTY terminal panel.
 *
 * Uses xterm.js to render a full terminal connected to the workspace
 * container via Socket.IO. Data is base64-encoded for safe transport.
 */

import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { useTerminalStore } from '@/stores/terminal'
import * as workspacesApi from '@/services/workspaces.api'
import {
  onEvent,
  onReconnect,
  sendTerminalInput,
  sendTerminalResize,
  sendTerminalClose,
} from '@/services/socket'
import { UiButton, UiSpinner } from '@/components/ui'
import { X, Minus } from 'lucide-vue-next'

const props = defineProps<{
  workspaceId: string
}>()

const terminalStore = useTerminalStore()

const terminalRef = ref<HTMLDivElement | null>(null)
const connecting = ref(false)
const error = ref<string | null>(null)

let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let resizeObserver: ResizeObserver | null = null
const cleanupFns: (() => void)[] = []
let pendingResize: { cols: number; rows: number } | null = null

// --- Helpers ---

function base64Encode(text: string): string {
  const encoder = new TextEncoder()
  const bytes = encoder.encode(text)
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary)
}

function base64Decode(b64: string): Uint8Array {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i)
  }
  return bytes
}

function queueResize(cols: number, rows: number): void {
  pendingResize = { cols, rows }
  flushPendingResize()
}

function flushPendingResize(): void {
  if (!pendingResize || !terminalStore.terminalId) {
    return
  }

  sendTerminalResize(
    props.workspaceId,
    terminalStore.terminalId,
    pendingResize.cols,
    pendingResize.rows,
  )
  pendingResize = null
}

async function fitTerminal(syncResize: boolean = true): Promise<void> {
  await nextTick()
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))

  fitAddon?.fit()

  if (syncResize && terminal) {
    queueResize(terminal.cols, terminal.rows)
  }
}

// --- Lifecycle ---

function initTerminal(): void {
  if (!terminalRef.value) return

  terminal = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', Menlo, monospace",
    theme: {
      background: '#0f1a23',
      foreground: '#f0ebd8',
      cursor: '#ff6d22',
      selectionBackground: 'rgba(255, 109, 34, 0.3)',
      black: '#172935',
      red: '#ef4444',
      green: '#22c55e',
      yellow: '#f59e0b',
      blue: '#3b82f6',
      magenta: '#a855f7',
      cyan: '#06b6d4',
      white: '#f0ebd8',
      brightBlack: '#5c6b73',
      brightRed: '#f87171',
      brightGreen: '#4ade80',
      brightYellow: '#fbbf24',
      brightBlue: '#60a5fa',
      brightMagenta: '#c084fc',
      brightCyan: '#22d3ee',
      brightWhite: '#ffffff',
    },
    scrollback: 5000,
    allowProposedApi: true,
  })

  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(terminalRef.value)

  // Send user input to backend
  terminal.onData((data: string) => {
    if (terminalStore.terminalId) {
      sendTerminalInput(
        props.workspaceId,
        terminalStore.terminalId,
        base64Encode(data),
      )
    }
  })

  // Watch for resize
  terminal.onResize(({ cols, rows }) => {
    queueResize(cols, rows)
  })

  // Observe container size changes
  resizeObserver = new ResizeObserver(() => {
    void fitTerminal(true)
  })
  resizeObserver.observe(terminalRef.value)
}

async function connectTerminal(): Promise<void> {
  connecting.value = true
  error.value = null

  try {
    await fitTerminal(true)
    const cols = terminal?.cols ?? 80
    const rows = terminal?.rows ?? 24
    await workspacesApi.startTerminal(props.workspaceId, cols, rows)
    // The terminal:started event will arrive via Socket.IO
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to start terminal'
    connecting.value = false
  }
}

function setupSocketListeners(): void {
  cleanupFns.push(
    onEvent('terminal:started', (data) => {
      if (data.workspace_id === props.workspaceId) {
        terminalStore.setConnected(data.terminal_id, props.workspaceId)
        flushPendingResize()
        void fitTerminal(true)
        connecting.value = false
        terminal?.focus()
      }
    }),
  )

  cleanupFns.push(
    onEvent('terminal:output', (data) => {
      if (
        data.workspace_id === props.workspaceId &&
        data.terminal_id === terminalStore.terminalId
      ) {
        const bytes = base64Decode(data.data)
        terminal?.write(bytes)
      }
    }),
  )

  cleanupFns.push(
    onEvent('terminal:closed', (data) => {
      if (
        data.workspace_id === props.workspaceId &&
        data.terminal_id === terminalStore.terminalId
      ) {
        terminalStore.setDisconnected()
        terminal?.writeln('\r\n\x1b[33m[Terminal session ended]\x1b[0m')
      }
    }),
  )

  // Auto-reconnect terminal when the WebSocket drops and comes back up.
  // The backend-side PTY session is gone after a reconnect, so we start a fresh one.
  cleanupFns.push(
    onReconnect(() => {
      if ((terminalStore.isConnected || connecting.value) && !connecting.value) {
        terminalStore.setDisconnected()
        terminal?.writeln('\r\n\x1b[33m[WebSocket reconnected — reconnecting terminal…]\x1b[0m')
        connectTerminal()
      }
    }),
  )
}

function cleanup(): void {
  // Remove Socket.IO listeners
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0

  // Close terminal on backend if connected
  if (terminalStore.terminalId) {
    sendTerminalClose(props.workspaceId, terminalStore.terminalId)
    terminalStore.setDisconnected()
  }

  // Dispose resize observer
  resizeObserver?.disconnect()
  resizeObserver = null

  // Dispose xterm
  terminal?.dispose()
  terminal = null
  fitAddon = null
  pendingResize = null
}

function terminateTerminalSession(): void {
  if (terminalStore.terminalId) {
    sendTerminalClose(props.workspaceId, terminalStore.terminalId)
    terminalStore.setDisconnected()
    terminal?.writeln('\r\n\x1b[33m[Terminal session ended]\x1b[0m')
  }
}

function handleMinimize(): void {
  terminalStore.minimize()
}

function handleClose(): void {
  terminateTerminalSession()
  terminalStore.close()
}

onMounted(() => {
  initTerminal()
  setupSocketListeners()
  connectTerminal()
})

onBeforeUnmount(() => {
  cleanup()
})

// Re-fit when panel becomes visible
watch(
  () => terminalStore.isOpen,
  (open) => {
    if (open) {
      nextTick(() => {
        void fitTerminal(true)
        terminal?.focus()
      })
    }
  },
)
</script>

<template>
  <div class="flex flex-col h-full min-w-0 bg-[#0f1a23] rounded-t-lg overflow-hidden relative">
    <!-- Terminal header bar -->
    <div
      class="flex items-center justify-between px-3 py-1.5 bg-[#172935] border-b border-[#2a4a5c] shrink-0"
    >
      <div class="flex items-center gap-2">
        <span class="text-xs font-medium text-[#94a3b8]">Terminal</span>
        <span
          v-if="terminalStore.isConnected"
          class="inline-block w-1.5 h-1.5 rounded-full bg-green-500"
        />
        <UiSpinner v-else-if="connecting" :size="12" />
        <span
          v-else
          class="inline-block w-1.5 h-1.5 rounded-full bg-red-500"
        />
      </div>
      <div class="flex items-center gap-1">
        <UiButton
          v-if="!terminalStore.isConnected && !connecting"
          size="sm"
          variant="ghost"
          class="text-[#94a3b8] hover:text-white text-xs h-6 px-2"
          @click="connectTerminal"
        >
          Reconnect
        </UiButton>
        <button
          class="p-1 rounded hover:bg-[#1e3545] text-[#94a3b8] hover:text-white transition-colors"
          title="Minimize"
          @click="handleMinimize"
        >
          <Minus :size="14" />
        </button>
        <button
          class="p-1 rounded hover:bg-[#1e3545] text-[#94a3b8] hover:text-white transition-colors"
          title="Close terminal"
          @click="handleClose"
        >
          <X :size="14" />
        </button>
      </div>
    </div>

    <!-- Terminal body -->
    <div ref="terminalRef" class="flex-1 min-h-0 min-w-0 p-1 overflow-hidden" />

    <!-- Error overlay -->
    <div
      v-if="error"
      class="absolute inset-0 flex items-center justify-center bg-[#0f1a23]/80"
    >
      <div class="text-center">
        <p class="text-red-400 text-sm mb-2">{{ error }}</p>
        <UiButton size="sm" variant="outline" @click="connectTerminal">
          Retry
        </UiButton>
      </div>
    </div>
  </div>
</template>
