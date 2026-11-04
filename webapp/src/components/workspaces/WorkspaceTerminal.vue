<script setup lang="ts">
/**
 * WorkspaceTerminal — interactive PTY terminal panel.
 *
 * Uses xterm.js to render a full terminal connected to the workspace
 * container via Socket.IO. Data is base64-encoded for safe transport.
 *
 * The component owns exactly one shell: it adopts only the shell of its own
 * start request and closes it on unmount, even when it starts afterwards.
 * The server also closes it once this connection ends or the shell idles.
 */

import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { useTerminalStore } from '@/stores/terminal'
import * as workspacesApi from '@/services/workspaces.api'
import {
  isConnected,
  getConnectionId,
  onEvent,
  onReconnect,
  subscribeToWorkspace,
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
const endedNotice = ref('Terminal session ended')

let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let resizeObserver: ResizeObserver | null = null
const cleanupFns: (() => void)[] = []
let pendingResize: { cols: number; rows: number } | null = null
// The shell this component owns; the shared store only mirrors its state.
let terminalId: string | null = null
// Each start attempt supersedes the previous one.
let attempt = 0
let request: TerminalRequest | null = null

const CLOSED_NOTICES: Record<string, string> = {
  idle: 'Terminal closed after inactivity',
  evicted: 'Terminal closed: too many open terminals',
}
// How long an abandoned start is watched so a late shell is still closed.
const ABANDONED_START_TIMEOUT_MS = 120_000

interface TerminalRequest {
  /** Resolves with the terminal id once the runner started this shell. */
  started: Promise<string>
  /** Stop waiting; a shell that still starts is closed on arrival. */
  abandon: () => void
}

/** Start a shell and follow only the events of this start request. */
function requestTerminal(workspaceId: string, cols: number, rows: number): TerminalRequest {
  // Events can arrive before the start request resolves; keep them by task.
  const early = new Map<string, { terminalId?: string; error?: string }>()
  let taskId: string | null = null
  let abandoned = false
  let settled = false
  let abandonTimer: ReturnType<typeof setTimeout> | null = null
  let resolveStarted!: (id: string) => void
  let rejectStarted!: (reason: Error) => void
  const started = new Promise<string>((resolve, reject) => {
    resolveStarted = resolve
    rejectStarted = reject
  })

  function settle(result: { terminalId?: string; error?: string }): void {
    if (settled) return
    settled = true
    offStarted()
    offError()
    if (abandonTimer) clearTimeout(abandonTimer)
    if (abandoned) {
      if (result.terminalId) sendTerminalClose(workspaceId, result.terminalId)
    } else if (result.terminalId) {
      resolveStarted(result.terminalId)
    } else {
      rejectStarted(new Error(result.error || 'Failed to start terminal'))
    }
  }

  function track(task: string | undefined, result: { terminalId?: string; error?: string }): void {
    if (!task) return
    if (taskId === null) early.set(task, result)
    else if (task === taskId) settle(result)
  }

  const offStarted = onEvent('terminal:started', (data) => {
    if (data.workspace_id === workspaceId) track(data.task_id, { terminalId: data.terminal_id })
  })
  // The runner reports a failed PTY start as workspace:error for the task.
  const offError = onEvent('workspace:error', (data) => {
    if (data.workspace_id === workspaceId) track(data.task_id, { error: data.error })
  })

  workspacesApi
    .startTerminal(workspaceId, cols, rows, getConnectionId())
    .then(({ task_id }) => {
      taskId = task_id
      const result = early.get(task_id)
      early.clear()
      if (result) settle(result)
    })
    .catch((err: unknown) => settle({ error: err instanceof Error ? err.message : undefined }))

  return {
    started,
    abandon: () => {
      if (settled || abandoned) return
      abandoned = true
      abandonTimer = setTimeout(() => settle({}), ABANDONED_START_TIMEOUT_MS)
    },
  }
}

function whenSocketConnected(): Promise<void> {
  if (isConnected.value) return Promise.resolve()
  return new Promise((resolve) => {
    const stop = watch(isConnected, (connected) => {
      if (!connected) return
      stop()
      resolve()
    })
  })
}

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
  if (!pendingResize || !terminalId) {
    return
  }

  sendTerminalResize(props.workspaceId, terminalId, pendingResize.cols, pendingResize.rows)
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
    if (terminalId) {
      sendTerminalInput(props.workspaceId, terminalId, base64Encode(data))
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
  const current = ++attempt
  request?.abandon()
  request = null
  connecting.value = true
  error.value = null

  try {
    // The shell is bound to this connection, so it must exist first.
    await whenSocketConnected()
    await fitTerminal(true)
    if (current !== attempt) return
    const own = requestTerminal(props.workspaceId, terminal?.cols ?? 80, terminal?.rows ?? 24)
    request = own
    const id = await own.started
    if (current !== attempt) return
    request = null
    adoptTerminal(id)
  } catch (err: unknown) {
    if (current !== attempt) return
    request = null
    error.value = err instanceof Error ? err.message : 'Failed to start terminal'
    connecting.value = false
  }
}

function adoptTerminal(id: string): void {
  terminalId = id
  terminalStore.setConnected(id, props.workspaceId)
  hadConnection.value = true
  connecting.value = false
  error.value = null
  flushPendingResize()
  void fitTerminal(true)
  terminal?.focus()
}

function releaseTerminal(): void {
  attempt += 1
  request?.abandon()
  request = null
  if (terminalId) sendTerminalClose(props.workspaceId, terminalId)
  terminalId = null
  terminalStore.setDisconnected()
}

function setupSocketListeners(): void {
  // Hold the room while mounted: output arrives there, and leaving it lets
  // the server release this connection's shell even if a close is lost.
  cleanupFns.push(subscribeToWorkspace(props.workspaceId))

  cleanupFns.push(
    onEvent('terminal:output', (data) => {
      if (data.workspace_id === props.workspaceId && data.terminal_id === terminalId) {
        const bytes = base64Decode(data.data)
        terminal?.write(bytes)
      }
    }),
  )

  cleanupFns.push(
    onEvent('terminal:closed', (data) => {
      if (data.workspace_id === props.workspaceId && data.terminal_id === terminalId) {
        terminalId = null
        terminalStore.setDisconnected()
        endedNotice.value = CLOSED_NOTICES[data.reason ?? ''] ?? 'Terminal session ended'
        terminal?.writeln(`\r\n\x1b[33m[${endedNotice.value}]\x1b[0m`)
      }
    }),
  )

  // The server closes this connection's shell when the WebSocket drops, so
  // start a fresh one once it is back.
  cleanupFns.push(
    onReconnect(() => {
      if (!terminalId && !connecting.value) return
      releaseTerminal()
      terminal?.writeln('\r\n\x1b[33m[WebSocket reconnected — reconnecting terminal…]\x1b[0m')
      void connectTerminal()
    }),
  )
}

function cleanup(): void {
  releaseTerminal()

  // Remove Socket.IO listeners and release the workspace room
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0

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
      <p class="text-sm text-muted-foreground">{{ endedNotice }}</p>
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
