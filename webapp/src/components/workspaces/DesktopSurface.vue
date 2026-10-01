<script setup lang="ts">
/**
 * DesktopSurface owns the one persistent KasmVNC iframe for a workspace.
 * The iframe stays at this component's fixed root placement; only that
 * placement is aligned with the active sidebar/modal viewport. Keeping the
 * browsing context stationary avoids WebKit/Chrome resetting VNC on moves.
 * This component also owns session lifecycle, viewer recovery, scaling,
 * computer-use overlay.
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
  createNativeClipboardContext,
  NATIVE_CLIPBOARD_ACTION,
  NATIVE_CLIPBOARD_VERSION,
  parseDesktopConnectionStatus,
  parseNativeClipboardMessage,
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
const clipboardHint = ref('')
const bridgeReady = ref(false)
let clipboardSequence = 0
let clipboardHintTimer: ReturnType<typeof setTimeout> | null = null
const parentFocused = ref(typeof document === 'undefined' ? false : document.hasFocus())
const modalVisible = ref(false)
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
let hostMutationObserver: MutationObserver | null = null
let iframeErrorCleanup: (() => void) | null = null
let blurTimer: ReturnType<typeof setTimeout> | null = null
const surfaceVisible = ref(false)
let bridgeConnectionConfirmed = false

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

function clearIframeErrorListener(): void {
  iframeErrorCleanup?.()
  iframeErrorCleanup = null
}

function bindDesktopIframeErrorListener(): void {
  clearIframeErrorListener()
  const iframe = desktopIframeRef.value
  const win = iframe?.contentWindow
  if (!iframe || !win) return

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

function isElementVisible(element: HTMLElement): boolean {
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    const style = window.getComputedStyle(node)
    if (style.display === 'none' || style.visibility === 'hidden' ||
        (style.opacity !== '' && Number(style.opacity) === 0))
      return false
  }
  return true
}

function sendClipboardContext(): void {
  const frame = desktopIframeRef.value
  if (!frame?.contentWindow || !frame.src) return
  let targetOrigin: string
  try {
    targetOrigin = new URL(frame.src, window.location.href).origin
  } catch {
    return
  }
  const visible = surfaceVisible.value && parentFocused.value && document.visibilityState === 'visible'
  const enabled = bridgeReady.value && Boolean(desktopStore.proxyUrl) &&
    desktopStore.isConnected && desktopStore.workspaceId === props.workspaceId
  frame.contentWindow.postMessage(
    createNativeClipboardContext(++clipboardSequence, props.workspaceId, {
      enabled,
      connected: enabled && bridgeConnectionConfirmed,
      visible,
      parentFocused: parentFocused.value && document.hasFocus(),
      computerUseActive: desktopStore.computerUseActive,
    }),
    targetOrigin,
  )
}

function showClipboardFallback(reason: 'unsupported' | 'permission' | 'unavailable'): void {
  if (desktopStore.computerUseActive || !surfaceVisible.value || !desktopStore.isConnected ||
      desktopStore.workspaceId !== props.workspaceId) return
  clipboardHint.value =
    reason === 'unsupported'
      ? 'Native clipboard is unavailable in this browser. Use the KasmVNC text clipboard.'
      : reason === 'permission'
        ? 'Clipboard access was denied. Use the KasmVNC text clipboard.'
        : 'Native clipboard could not be used. Use the KasmVNC text clipboard.'
  if (clipboardHintTimer) clearTimeout(clipboardHintTimer)
  clipboardHintTimer = setTimeout(() => {
    clipboardHint.value = ''
    clipboardHintTimer = null
  }, 12_000)
}

function handleNativeClipboardMessage(event: MessageEvent): boolean {
  const iframe = desktopIframeRef.value
  if (!isTrustedDesktopMessage(event, iframe, window.location.href)) return false
  const message = parseNativeClipboardMessage(event.data)
  if (!message) return false
  if (message.kind === 'ready') {
    bridgeReady.value = true
    sendClipboardContext()
  } else if (message.kind === 'fallback') {
    if (bridgeReady.value && desktopStore.workspaceId === props.workspaceId)
      showClipboardFallback(message.reason)
  }
  return true
}

function requestNativeClipboardPanel(): void {
  const frame = desktopIframeRef.value
  if (!frame?.contentWindow || !frame.src || desktopStore.computerUseActive ||
      !surfaceVisible.value || !bridgeReady.value || !bridgeConnectionConfirmed ||
      !desktopStore.isConnected || desktopStore.workspaceId !== props.workspaceId ||
      document.visibilityState !== 'visible' || !document.hasFocus()) return
  frame.focus()
  try {
    frame.contentWindow.focus()
  } catch {
    // Cross-origin Kasm clients cannot be focused through their WindowProxy.
  }
  parentFocused.value = true
  sendClipboardContext()
  try {
    frame.contentWindow.postMessage(
      { action: NATIVE_CLIPBOARD_ACTION, version: NATIVE_CLIPBOARD_VERSION, kind: 'open-panel' },
      new URL(frame.src, window.location.href).origin,
    )
  } catch {
    // A stale or invalid proxy URL simply leaves the concise hint visible.
  }
}

function refreshHostBounds(): void {
  hostResizeObserver?.disconnect()
  hostMutationObserver?.disconnect()
  hostResizeObserver = null
  hostMutationObserver = null
  const host = activeHost()
  modalVisible.value = Boolean(desktopStore.isOpen && host)
  if (!host || !surfaceRef.value) {
    surfaceVisible.value = false
    viewportWidth.value = 0
    viewportHeight.value = 0
    surfaceStyle.value = {
      ...surfaceStyle.value,
      left: '-10000px',
      visibility: 'hidden',
      pointerEvents: 'none',
    }
    sendClipboardContext()
    return
  }

  const update = () => {
    if (activeHost() !== host || !surfaceRef.value) {
      surfaceVisible.value = false
      sendClipboardContext()
      return
    }
    const rect = host.getBoundingClientRect()
    const style = window.getComputedStyle(host)
    const intersectsViewport =
      rect.right > 0 &&
      rect.bottom > 0 &&
      rect.left < window.innerWidth &&
      rect.top < window.innerHeight
    const visible =
      isElementVisible(host) &&
      style.display !== 'none' &&
      rect.width > 0 &&
      rect.height > 0 &&
      intersectsViewport
    surfaceVisible.value = visible
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
    sendClipboardContext()
  }

  update()
  hostResizeObserver = new ResizeObserver(update)
  hostResizeObserver.observe(host)
  hostMutationObserver = new MutationObserver(update)
  for (let node: HTMLElement | null = host; node; node = node.parentElement)
    hostMutationObserver.observe(node, { attributes: true, attributeFilter: ['class', 'style', 'hidden'] })
}

function onViewportChange(): void {
  refreshHostBounds()
}

function isParentDesktopFocus(): boolean {
  if (!document.hasFocus()) return false
  const active = document.activeElement
  if (active === desktopIframeRef.value) return true
  if (!(active instanceof HTMLElement)) return true
  if (active.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]')) return false
  const host = activeHost()
  return active === document.body || active === document.documentElement || Boolean(host?.contains(active))
}

function updateParentFocus(): void {
  parentFocused.value = isParentDesktopFocus()
  sendClipboardContext()
}

function onWindowBlur(): void {
  if (blurTimer) clearTimeout(blurTimer)
  parentFocused.value = false
  sendClipboardContext()
  blurTimer = setTimeout(() => {
    blurTimer = null
    if (document.hasFocus()) updateParentFocus()
  }, 0)
}

function onWindowFocus(): void {
  updateParentFocus()
}

function onParentFocusIn(): void {
  updateParentFocus()
}

function onParentFocusOut(): void {
  queueMicrotask(updateParentFocus)
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
  bridgeConnectionConfirmed = true
  iframeLoaded.value = true
  sendClipboardContext()
  recoveryError.value = false
  connectionTimeout.cancel()
  reconnectBackoff.reset()
  bindDesktopIframeErrorListener()
}

function markViewerDisconnected(): void {
  if (!desktopStore.isConnected) return
  bridgeConnectionConfirmed = false
  sendClipboardContext()
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
  bindDesktopIframeErrorListener()
  if (readIframeConnection()) {
    markViewerConnected()
    return
  }
  // A document load is not proof of a live KasmVNC websocket.
  viewerStatus.value = 'connecting'
  startConnectionTimeout()
}

function handleWindowMessage(event: MessageEvent): void {
  if (handleNativeClipboardMessage(event)) return
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
  updateParentFocus()
  if (document.visibilityState !== 'visible') {
    sendClipboardContext()
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

onMounted(() => {
  if (desktopStore.workspaceId && desktopStore.workspaceId !== props.workspaceId)
    desktopStore.reset()
  setupSocketListeners()
  if (desktopStore.isOpen && !desktopStore.isConnected && !desktopStore.isConnecting)
    void startDesktop()
  window.addEventListener('message', handleWindowMessage)
  window.addEventListener('focus', onWindowFocus)
  window.addEventListener('blur', onWindowBlur)
  document.addEventListener('focusin', onParentFocusIn, true)
  document.addEventListener('focusout', onParentFocusOut, true)
  window.addEventListener('resize', onViewportChange)
  window.addEventListener('scroll', onViewportChange, true)
  document.addEventListener('visibilitychange', onVisibilityChange)
  updateParentFocus()
  refreshHostBounds()
})

onBeforeUnmount(() => {
  void stopDesktopIfActive(props.workspaceId)
  cleanupSocketListeners()
  hostResizeObserver?.disconnect()
  hostResizeObserver = null
  hostMutationObserver?.disconnect()
  hostMutationObserver = null
  clearIframeErrorListener()
  if (blurTimer) clearTimeout(blurTimer)
  if (clipboardHintTimer) clearTimeout(clipboardHintTimer)
  connectionTimeout.cancel()
  reconnectBackoff.reset()
  window.removeEventListener('message', handleWindowMessage)
  window.removeEventListener('focus', onWindowFocus)
  window.removeEventListener('blur', onWindowBlur)
  document.removeEventListener('focusin', onParentFocusIn, true)
  document.removeEventListener('focusout', onParentFocusOut, true)
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


watch(
  () => [desktopStore.proxyUrl, desktopStore.viewerGeneration] as const,
  async () => {
    bridgeReady.value = false
    bridgeConnectionConfirmed = false
    clipboardHint.value = ''
    iframeLoaded.value = false
    viewerStatus.value = 'connecting'
    connectionTimeout.cancel()
    clearIframeErrorListener()
    await nextTick()
    if (desktopIframeRef.value) startConnectionTimeout()
  },
)

watch(
  () => [desktopStore.isConnected, desktopStore.computerUseActive, desktopStore.workspaceId] as const,
  () => {
    if (desktopStore.workspaceId !== props.workspaceId || !desktopStore.isConnected) {
      bridgeConnectionConfirmed = false
      clipboardHint.value = ''
    }
    if (desktopStore.computerUseActive) clipboardHint.value = ''
    sendClipboardContext()
  },
  { flush: 'sync' },
)

watch([surfaceRef, sidebarDesktopHost, modalDesktopHost, () => desktopStore.isOpen], refreshHostBounds)
watch(desktopIframeRef, () => {
  bridgeReady.value = false
  bridgeConnectionConfirmed = false
  clipboardHint.value = ''
  clearIframeErrorListener()
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
      v-if="clipboardHint && !desktopStore.computerUseActive && surfaceVisible && (desktopStore.isOpen ? modalVisible : !desktopStore.isOpen)"
      class="absolute bottom-3 left-1/2 z-25 flex w-[calc(100%-1.5rem)] max-w-xl -translate-x-1/2 flex-wrap items-center gap-2 rounded-md bg-card px-3 py-2 text-xs text-foreground shadow-lg"
      role="status"
      data-testid="desktop-clipboard-fallback"
    >
      <span class="min-w-0 flex-1 basis-full sm:basis-auto">{{ clipboardHint }}</span>
      <Button class="shrink-0" size="sm" variant="outline" @click="requestNativeClipboardPanel">
        Open KasmVNC clipboard
      </Button>
      <Button class="shrink-0" size="sm" variant="ghost" aria-label="Dismiss clipboard notice" @click="clipboardHint = ''">
        Dismiss
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
