<script setup lang="ts">
/**
 * DesktopSurface owns the one persistent KasmVNC iframe for a workspace.
 * The iframe stays at this component's fixed root placement; only that
 * placement is aligned with the active sidebar/modal viewport. Keeping the
 * browsing context stationary avoids WebKit/Chrome resetting VNC on moves.
 * This component also owns session lifecycle, viewer recovery, scaling,
 * computer-use overlay and clipboard shortcuts.
 */
import { ref, computed, onMounted, onBeforeUnmount, watch, toRef, nextTick } from 'vue'
import type { CSSProperties } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useDesktopSession } from '@/composables/useDesktopSession'
import { getConfig } from '@/services/config'
import {
  desktopIframeSrc as buildDesktopIframeSrc,
  workspaceDesktopSize,
} from '@/lib/desktopGeometry'
import { sidebarDesktopHost, modalDesktopHost } from '@/lib/desktopSurfaceHost'
import {
  createDesktopReconnectBackoff,
  createPausableTimer,
  isTrustedDesktopMessage,
  parseDesktopConnectionStatus,
} from '@/lib/desktopSurfaceRecovery'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { MousePointerClick } from '@lucide/vue'

const props = defineProps<{ workspaceId: string }>()

const desktopStore = useDesktopStore()
const workspaceStore = useWorkspaceStore()
const {
  error,
  takeControlBusy,
  startDesktop,
  stopDesktopIfActive,
  takeControl,
  copyFromVmClipboard,
  pasteToVmClipboard,
  setupSocketListeners,
  cleanupSocketListeners,
} = useDesktopSession(toRef(props, 'workspaceId'))

const surfaceRef = ref<HTMLElement | null>(null)
const desktopIframeRef = ref<HTMLIFrameElement | null>(null)
const viewportWidth = ref(0)
const viewportHeight = ref(0)
const iframeLoaded = ref(false)
const viewerStatus = ref<'connecting' | 'connected' | 'disconnected'>('connecting')
const recoveryError = ref(false)
const surfaceStyle = ref<CSSProperties>({
  position: 'fixed',
  left: '-10000px',
  top: '0',
  width: '1px',
  height: '1px',
  zIndex: 60,
  visibility: 'hidden',
  pointerEvents: 'none',
})
let hostResizeObserver: ResizeObserver | null = null
let iframeKeydownCleanup: (() => void) | null = null
let iframeErrorCleanup: (() => void) | null = null
let isDispatchingSyntheticPasteShortcut = false

const desktopSize = computed(() => {
  const workspace =
    workspaceStore.workspaces.find((entry) => entry.id === props.workspaceId) ??
    (workspaceStore.activeWorkspace?.id === props.workspaceId
      ? workspaceStore.activeWorkspace
      : null)
  return workspaceDesktopSize(workspace)
})

const viewportScale = computed(() => {
  if (viewportWidth.value <= 0 || viewportHeight.value <= 0) return 1
  const fitScale = Math.min(
    viewportWidth.value / Math.max(desktopSize.value.width, 1),
    viewportHeight.value / Math.max(desktopSize.value.height, 1),
  )
  return Math.max(Math.min(fitScale, 1), 0.1)
})

const scaledFrameStyle = computed<CSSProperties>(() => ({
  width: `${desktopSize.value.width}px`,
  height: `${desktopSize.value.height}px`,
  transform: `scale(${viewportScale.value})`,
  transformOrigin: 'center center',
}))

const scaledIframeStyle = computed<CSSProperties>(() => ({
  width: `${desktopSize.value.width}px`,
  height: `${desktopSize.value.height}px`,
}))

const desktopIframeSrc = computed(() => {
  if (!desktopStore.proxyUrl) return ''
  const token = localStorage.getItem('kern_access_token') || ''
  const config = getConfig()
  return buildDesktopIframeSrc(config.wsBaseUrl || '', desktopStore.proxyUrl, token)
})

// --- Clipboard shortcuts (Cmd/Ctrl+C/V synced with the VM clipboard) ---

function shouldHandleClipboardShortcut(event: KeyboardEvent): boolean {
  if (!desktopStore.isConnected) return false
  const target = event.target as HTMLElement | null
  if (target?.closest('input, textarea, [contenteditable="true"]')) return false
  return true
}

function parseClipboardShortcut(event: KeyboardEvent): 'copy' | 'paste' | null {
  const key = event.key.toLowerCase()
  const modifierPressed = event.metaKey || event.ctrlKey
  if (!modifierPressed || event.altKey || event.shiftKey) return null
  if (key === 'c') return 'copy'
  if (key === 'v') return 'paste'
  return null
}

function suppressClipboardEvent(event: KeyboardEvent): void {
  event.preventDefault()
  event.stopPropagation()
  event.stopImmediatePropagation()
}

function dispatchPasteShortcutToVm(event: KeyboardEvent): void {
  const iframe = desktopIframeRef.value
  const doc = iframe?.contentDocument
  if (!iframe || !doc) return

  const activeTarget = (doc.activeElement as HTMLElement | null) ?? doc.body ?? doc.documentElement
  if (!activeTarget) return

  const modifierKey = event.metaKey ? 'Meta' : 'Control'
  const shortcutEventInit: KeyboardEventInit = {
    key: 'v',
    code: 'KeyV',
    bubbles: true,
    cancelable: true,
    composed: true,
    ctrlKey: modifierKey === 'Control',
    metaKey: modifierKey === 'Meta',
  }

  isDispatchingSyntheticPasteShortcut = true
  try {
    activeTarget.dispatchEvent(
      new KeyboardEvent('keydown', { key: modifierKey, code: `${modifierKey}Left`, bubbles: true }),
    )
    activeTarget.dispatchEvent(new KeyboardEvent('keydown', shortcutEventInit))
    activeTarget.dispatchEvent(new KeyboardEvent('keyup', shortcutEventInit))
    activeTarget.dispatchEvent(
      new KeyboardEvent('keyup', { key: modifierKey, code: `${modifierKey}Left`, bubbles: true }),
    )
  } finally {
    isDispatchingSyntheticPasteShortcut = false
  }
}

async function handleDesktopIframeKeydown(event: KeyboardEvent): Promise<void> {
  if (isDispatchingSyntheticPasteShortcut || !shouldHandleClipboardShortcut(event)) return
  const shortcut = parseClipboardShortcut(event)
  if (!shortcut) return

  if (shortcut === 'copy') {
    window.setTimeout(() => void copyFromVmClipboard(), 120)
    return
  }

  suppressClipboardEvent(event)
  if (await pasteToVmClipboard()) dispatchPasteShortcutToVm(event)
}

function handleDesktopIframeKeyup(event: KeyboardEvent): void {
  if (isDispatchingSyntheticPasteShortcut || !shouldHandleClipboardShortcut(event)) return
  if (parseClipboardShortcut(event) === 'paste') suppressClipboardEvent(event)
}

function clearIframeListeners(): void {
  iframeKeydownCleanup?.()
  iframeKeydownCleanup = null
  iframeErrorCleanup?.()
  iframeErrorCleanup = null
}

function bindDesktopIframeListeners(): void {
  clearIframeListeners()
  const iframe = desktopIframeRef.value
  const doc = iframe?.contentDocument
  const win = iframe?.contentWindow
  if (!iframe || !doc || !win) return
  const keydownListener = (event: KeyboardEvent) => void handleDesktopIframeKeydown(event)
  const keyupListener = (event: KeyboardEvent) => handleDesktopIframeKeyup(event)
  win.addEventListener('keydown', keydownListener, true)
  win.addEventListener('keyup', keyupListener, true)
  doc.addEventListener('keydown', keydownListener, true)
  doc.addEventListener('keyup', keyupListener, true)
  iframeKeydownCleanup = () => {
    win.removeEventListener('keydown', keydownListener, true)
    win.removeEventListener('keyup', keyupListener, true)
    doc.removeEventListener('keydown', keydownListener, true)
    doc.removeEventListener('keyup', keyupListener, true)
  }

  const onFrameError = (event: ErrorEvent) => {
    // Never log the raw message or URL: either may contain the desktop token.
    // Script basename and coordinates identify parser failures without secrets.
    let script = '[unknown]'
    try {
      const pathname = new URL(event.filename).pathname
      const match = pathname.match(/\/(?:dist|vendor|core|app)\/([\w.~-]+\.js)$/)
      if (match) script = match[0].slice(1)
    } catch {
      // Inline and blob scripts have no safe vendor pathname.
    }
    console.error('Desktop viewer script error', {
      kind: event.error instanceof SyntaxError ? 'SyntaxError' : 'JavaScriptError',
      script,
      line: Number.isFinite(event.lineno) ? event.lineno : 0,
      column: Number.isFinite(event.colno) ? event.colno : 0,
    })
  }
  win.addEventListener('error', onFrameError)
  iframeErrorCleanup = () => win.removeEventListener('error', onFrameError)
}

// --- Fixed placement over the active viewport, without moving the iframe ---

function activeHost(): HTMLElement | null {
  return desktopStore.isOpen ? modalDesktopHost.value : sidebarDesktopHost.value
}

function refreshHostBounds(): void {
  hostResizeObserver?.disconnect()
  hostResizeObserver = null
  const host = activeHost()
  if (!host || !surfaceRef.value) {
    viewportWidth.value = 0
    viewportHeight.value = 0
    surfaceStyle.value = {
      ...surfaceStyle.value,
      left: '-10000px',
      visibility: 'hidden',
      pointerEvents: 'none',
    }
    return
  }

  const update = () => {
    if (activeHost() !== host || !surfaceRef.value) return
    const rect = host.getBoundingClientRect()
    const style = window.getComputedStyle(host)
    const intersectsViewport =
      rect.right > 0 &&
      rect.bottom > 0 &&
      rect.left < window.innerWidth &&
      rect.top < window.innerHeight
    const visible =
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      rect.width > 0 &&
      rect.height > 0 &&
      intersectsViewport
    viewportWidth.value = visible ? rect.width : 0
    viewportHeight.value = visible ? rect.height : 0
    surfaceStyle.value = {
      position: 'fixed',
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      zIndex: 60,
      visibility: visible ? 'visible' : 'hidden',
      pointerEvents: visible ? 'auto' : 'none',
    }
  }

  update()
  hostResizeObserver = new ResizeObserver(update)
  hostResizeObserver.observe(host)
}

function onViewportChange(): void {
  refreshHostBounds()
}

// --- KasmVNC status, recovery and page visibility ---

const connectionTimeout = createPausableTimer(() => {
  if (viewerStatus.value === 'connected') return
  markViewerDisconnected()
})

function startConnectionTimeout(): void {
  if (!connectionTimeout.pending) connectionTimeout.start(15_000)
  if (document.visibilityState !== 'visible') connectionTimeout.pause()
}

const reconnectBackoff = createDesktopReconnectBackoff(
  () => {
    if (document.visibilityState !== 'visible') return
    viewerStatus.value = 'connecting'
    recoveryError.value = false
    iframeLoaded.value = false
    desktopStore.bumpViewer()
  },
  () => {
    recoveryError.value = true
  },
)

function markViewerConnected(): void {
  viewerStatus.value = 'connected'
  iframeLoaded.value = true
  recoveryError.value = false
  connectionTimeout.cancel()
  reconnectBackoff.reset()
  bindDesktopIframeListeners()
}

function markViewerDisconnected(): void {
  if (!desktopStore.isConnected) return
  connectionTimeout.cancel()
  viewerStatus.value = 'disconnected'
  iframeLoaded.value = false
  reconnectBackoff.schedule()
  if (document.visibilityState !== 'visible') reconnectBackoff.pause()
}

function readIframeConnection(): boolean {
  try {
    return Boolean(
      desktopIframeRef.value?.contentDocument?.documentElement.classList.contains(
        'noVNC_connected',
      ),
    )
  } catch {
    return false
  }
}

function handleIframeLoad(event: Event): void {
  if (event.currentTarget !== desktopIframeRef.value) return
  bindDesktopIframeListeners()
  if (readIframeConnection()) {
    markViewerConnected()
    return
  }
  // A document load is not proof of a live KasmVNC websocket.
  viewerStatus.value = 'connecting'
  startConnectionTimeout()
}

function handleWindowMessage(event: MessageEvent): void {
  const iframe = desktopIframeRef.value
  if (!isTrustedDesktopMessage(event, iframe, window.location.href)) return
  const status = parseDesktopConnectionStatus(event.data)
  if (!status) return
  if (status === 'connected') markViewerConnected()
  else if (status === 'disconnected') markViewerDisconnected()
  else if (viewerStatus.value !== 'connected') {
    viewerStatus.value = 'connecting'
    startConnectionTimeout()
  }
}

function onVisibilityChange(): void {
  if (document.visibilityState !== 'visible') {
    connectionTimeout.pause()
    reconnectBackoff.pause()
    return
  }

  if (readIframeConnection()) {
    // A healthy frame survives backgrounding; do not reload it.
    markViewerConnected()
    return
  }
  connectionTimeout.resume()
  reconnectBackoff.resume()
  if (viewerStatus.value === 'connected') markViewerDisconnected()
}

function retryViewer(): void {
  connectionTimeout.cancel()
  recoveryError.value = false
  viewerStatus.value = 'connecting'
  iframeLoaded.value = false
  reconnectBackoff.retryNow()
  startConnectionTimeout()
}

function onGlobalKeydown(event: KeyboardEvent): void {
  if (!desktopStore.isOpen || !shouldHandleClipboardShortcut(event)) return
  const shortcut = parseClipboardShortcut(event)
  if (!shortcut) return
  event.preventDefault()
  if (shortcut === 'copy') void copyFromVmClipboard()
  else void pasteToVmClipboard()
}

onMounted(() => {
  if (desktopStore.workspaceId && desktopStore.workspaceId !== props.workspaceId)
    desktopStore.reset()
  setupSocketListeners()
  if (desktopStore.isOpen && !desktopStore.isConnected && !desktopStore.isConnecting)
    void startDesktop()
  window.addEventListener('keydown', onGlobalKeydown)
  window.addEventListener('message', handleWindowMessage)
  window.addEventListener('resize', onViewportChange)
  window.addEventListener('scroll', onViewportChange, true)
  document.addEventListener('visibilitychange', onVisibilityChange)
  refreshHostBounds()
})

onBeforeUnmount(() => {
  void stopDesktopIfActive(props.workspaceId)
  cleanupSocketListeners()
  hostResizeObserver?.disconnect()
  hostResizeObserver = null
  clearIframeListeners()
  connectionTimeout.cancel()
  reconnectBackoff.reset()
  window.removeEventListener('keydown', onGlobalKeydown)
  window.removeEventListener('message', handleWindowMessage)
  window.removeEventListener('resize', onViewportChange)
  window.removeEventListener('scroll', onViewportChange, true)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

watch(
  () => desktopStore.isOpen,
  (open) => {
    refreshHostBounds()
    if (open && !desktopStore.isConnected && !desktopStore.isConnecting) void startDesktop()
  },
)

watch([sidebarDesktopHost, modalDesktopHost], refreshHostBounds)

watch(
  () => [desktopStore.proxyUrl, desktopStore.viewerGeneration] as const,
  async () => {
    iframeLoaded.value = false
    viewerStatus.value = 'connecting'
    connectionTimeout.cancel()
    clearIframeListeners()
    await nextTick()
    if (desktopIframeRef.value) startConnectionTimeout()
  },
)

watch(surfaceRef, refreshHostBounds)
watch(desktopIframeRef, () => {
  clearIframeListeners()
  if (desktopIframeRef.value) startConnectionTimeout()
})
</script>

<template>
  <div
    v-if="desktopStore.isConnected && desktopStore.proxyUrl"
    ref="surfaceRef"
    class="fixed overflow-hidden"
    :style="surfaceStyle"
    data-testid="desktop-surface"
  >
    <div
      class="absolute inset-0 flex h-full w-full items-center justify-center overflow-hidden"
      :class="{ 'p-2': !desktopStore.isOpen }"
    >
      <div
        class="shrink-0 overflow-hidden rounded-[var(--radius-xs)] border border-border bg-black shadow-sm"
        :style="scaledFrameStyle"
      >
        <iframe
          :key="`${desktopStore.proxyUrl}:${desktopStore.viewerGeneration}`"
          ref="desktopIframeRef"
          :src="desktopIframeSrc"
          title="Desktop"
          class="block border-0 transition-opacity duration-200"
          :class="{ 'opacity-0': !iframeLoaded }"
          :style="scaledIframeStyle"
          sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
          allow="clipboard-read; clipboard-write"
          data-testid="desktop-surface-iframe"
          @load="handleIframeLoad"
        />
      </div>
    </div>

    <div
      v-if="!iframeLoaded"
      class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-card"
      data-testid="desktop-surface-loading"
    >
      <LoadingSpinner v-if="viewerStatus === 'connecting' && !recoveryError" :size="24" />
      <span
        v-if="viewerStatus === 'connecting' && !recoveryError"
        class="text-sm text-muted-foreground"
      >
        Connecting to desktop…
      </span>
      <span v-else class="text-center text-sm text-destructive">
        {{ recoveryError ? 'Desktop connection failed.' : 'Desktop disconnected. Reconnecting…' }}
      </span>
      <Button
        v-if="viewerStatus === 'disconnected' || recoveryError"
        size="sm"
        data-testid="desktop-surface-retry"
        @click="retryViewer"
      >
        Retry
      </Button>
    </div>

    <div
      v-if="desktopStore.computerUseActive"
      class="absolute inset-0 z-30 flex flex-col items-center justify-center gap-3 bg-black/55 px-4 text-center"
      tabindex="0"
      @keydown.prevent
    >
      <MousePointerClick :size="28" class="text-white" />
      <p class="max-w-md text-sm text-white">
        Computer-use is controlling this desktop. Watching is read-only. Taking control aborts the
        computer-use agent.
      </p>
      <Button size="sm" :disabled="takeControlBusy" @click="takeControl"> Take control </Button>
    </div>
  </div>
</template>
