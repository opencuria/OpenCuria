<script setup lang="ts">
/**
 * WorkspaceTerminal — interactive PTY terminal panel.
 *
 * Uses xterm.js to render a full terminal connected to the workspace
 * container via Socket.IO. Data is base64-encoded for safe transport.
 */

import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
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
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { TerminalSquare } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const terminalStore = useTerminalStore()

const terminalRef = ref<HTMLDivElement | null>(null)
const connecting = ref(false)
const error = ref<string | null>(null)
const hadConnection = ref(false)

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

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function initTerminal(): void {
  if (!terminalRef.value) return

  // Match the surrounding panel: background/foreground/cursor follow the
  // active design tokens instead of a bespoke terminal palette.
  const background = cssVar('--card', '#1c1c1f')
  const foreground = cssVar('--card-foreground', '#fafafa')
  const cursor = cssVar('--primary', '#ff6d22')

  terminal = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', Menlo, monospace",
    theme: {
      background,
      foreground,
      cursor,
      selectionBackground: 'rgba(255, 109, 34, 0.3)',
      black: cssVar('--muted', '#27272a'),
      red: '#ef4444',
      green: '#22c55e',
      yellow: '#f59e0b',
      blue: '#3b82f6',
      magenta: '#a855f7',
      cyan: '#06b6d4',
      white: foreground,
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
        hadConnection.value = true
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

onMounted(() => {
  initTerminal()
  setupSocketListeners()
  connectTerminal()
})

onBeforeUnmount(() => {
  cleanup()
})
</script>

<template>
  <div
    class="relative flex h-full min-w-0 flex-col overflow-hidden bg-card"
    data-testid="workspace-terminal"
  >
    <!-- Terminal body fills the whole tab -->
    <div ref="terminalRef" class="min-h-0 min-w-0 flex-1 overflow-hidden p-1.5" />

    <!-- Connecting overlay -->
    <div
      v-if="connecting && !hadConnection"
      class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-card/80"
    >
      <LoadingSpinner :size="20" />
      <span class="text-xs text-muted-foreground">Starting terminal…</span>
    </div>

    <!-- Session ended overlay -->
    <div
      v-else-if="hadConnection && !terminalStore.isConnected && !connecting && !error"
      class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-card/80"
    >
      <TerminalSquare :size="28" class="text-muted-foreground" />
      <p class="text-sm text-muted-foreground">Terminal session ended</p>
      <Button size="sm" data-testid="terminal-reconnect" @click="connectTerminal">
        Reconnect
      </Button>
    </div>

    <!-- Error overlay -->
    <div
      v-else-if="error"
      class="absolute inset-0 flex items-center justify-center bg-card/80"
    >
      <div class="text-center">
        <p class="mb-2 text-sm text-destructive">{{ error }}</p>
        <Button size="sm" variant="outline" @click="connectTerminal">
          Retry
        </Button>
      </div>
    </div>
  </div>
</template>
