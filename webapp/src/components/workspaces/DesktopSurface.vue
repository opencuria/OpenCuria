<script setup lang="ts">
/**
 * DesktopSurface — the single persistent KasmVNC iframe for a workspace.
 *
 * The iframe is created once per session and moved between the side-panel
 * host and the desktop modal host via <Teleport>, so opening or closing
 * the modal never reloads the VNC connection. This component owns the
 * session lifecycle (socket listeners, auto-start when the modal opens,
 * stop on unmount), the scale-to-fit rendering, the computer-use overlay
 * and the clipboard shortcuts. Until the KasmVNC page has loaded, the
 * iframe is hidden behind a themed placeholder so the KasmVNC loading
 * screen is never visible.
 */
import { ref, computed, onMounted, onBeforeUnmount, watch, toRef } from 'vue'
import type { CSSProperties } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useDesktopSession } from '@/composables/useDesktopSession'
import { getConfig } from '@/services/config'
import { desktopIframeSrc as buildDesktopIframeSrc, workspaceDesktopSize } from '@/lib/desktopGeometry'
import { sidebarDesktopHost, modalDesktopHost } from '@/lib/desktopSurfaceHost'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { MousePointerClick } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

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

const hiddenHostRef = ref<HTMLElement | null>(null)
const surfaceRef = ref<HTMLElement | null>(null)
const desktopIframeRef = ref<HTMLIFrameElement | null>(null)
const viewportWidth = ref(0)
const viewportHeight = ref(0)
const iframeLoaded = ref(false)
let resizeObserver: ResizeObserver | null = null
let iframeKeydownCleanup: (() => void) | null = null
let isDispatchingSyntheticPasteShortcut = false

// Move the iframe into the modal host while the modal is open, otherwise
// into the side-panel host. The offscreen fallback keeps the session alive
// when no visible host exists (e.g. side panel never opened).
const teleportTarget = computed<HTMLElement | null>(() => {
  if (desktopStore.isOpen) {
    return modalDesktopHost.value ?? hiddenHostRef.value
  }
  return sidebarDesktopHost.value ?? hiddenHostRef.value
})

const desktopSize = computed(() => {
  const workspace =
    workspaceStore.workspaces.find((entry) => entry.id === props.workspaceId)
    ?? (workspaceStore.activeWorkspace?.id === props.workspaceId
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
  const base = config.wsBaseUrl || ''
  return buildDesktopIframeSrc(base, desktopStore.proxyUrl, token)
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
  if (isDispatchingSyntheticPasteShortcut) return
  if (!shouldHandleClipboardShortcut(event)) return
  const shortcut = parseClipboardShortcut(event)
  if (!shortcut) return

  if (shortcut === 'copy') {
    window.setTimeout(() => {
      void copyFromVmClipboard()
    }, 120)
    return
  }

  suppressClipboardEvent(event)
  const synced = await pasteToVmClipboard()
  if (!synced) return
  dispatchPasteShortcutToVm(event)
}

function handleDesktopIframeKeyup(event: KeyboardEvent): void {
  if (isDispatchingSyntheticPasteShortcut) return
  if (!shouldHandleClipboardShortcut(event)) return
  if (parseClipboardShortcut(event) !== 'paste') return
  suppressClipboardEvent(event)
}

function bindDesktopIframeKeydownListener(): void {
  iframeKeydownCleanup?.()
  iframeKeydownCleanup = null

  const doc = desktopIframeRef.value?.contentDocument
  const win = desktopIframeRef.value?.contentWindow
  if (!doc || !win) return
  const keydownListener = (event: KeyboardEvent) => {
    void handleDesktopIframeKeydown(event)
  }
  const keyupListener = (event: KeyboardEvent) => {
    handleDesktopIframeKeyup(event)
  }
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
}

// Global handler only while the modal is open: with the embedded sidebar
// view, focus outside the iframe means the user is working elsewhere.
function onGlobalKeydown(event: KeyboardEvent): void {
  if (!desktopStore.isOpen) return
  if (!shouldHandleClipboardShortcut(event)) return
  const shortcut = parseClipboardShortcut(event)
  if (!shortcut) return

  if (shortcut === 'copy') {
    event.preventDefault()
    void copyFromVmClipboard()
  } else if (shortcut === 'paste') {
    event.preventDefault()
    void pasteToVmClipboard()
  }
}

// --- Scaling / load state ---

function observeSurface(): void {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (!surfaceRef.value) return
  const el = surfaceRef.value
  const refreshBounds = () => {
    const rect = el.getBoundingClientRect()
    viewportWidth.value = rect.width
    viewportHeight.value = rect.height
  }
  refreshBounds()
  resizeObserver = new ResizeObserver(refreshBounds)
  resizeObserver.observe(el)
}

function handleIframeLoad(): void {
  iframeLoaded.value = true
  bindDesktopIframeKeydownListener()
}

onMounted(() => {
  if (desktopStore.workspaceId && desktopStore.workspaceId !== props.workspaceId) {
    desktopStore.reset()
  }

  setupSocketListeners()

  if (desktopStore.isOpen && !desktopStore.isConnected && !desktopStore.isConnecting) {
    void startDesktop()
  }
  window.addEventListener('keydown', onGlobalKeydown)
})

onBeforeUnmount(() => {
  void stopDesktopIfActive(props.workspaceId)
  cleanupSocketListeners()
  resizeObserver?.disconnect()
  resizeObserver = null
  iframeKeydownCleanup?.()
  iframeKeydownCleanup = null
  window.removeEventListener('keydown', onGlobalKeydown)
})

watch(
  () => desktopStore.isOpen,
  (open) => {
    if (open && !desktopStore.isConnected && !desktopStore.isConnecting) {
      void startDesktop()
    }
  },
)

watch(
  () => props.workspaceId,
  async (workspaceId, previousWorkspaceId) => {
    if (workspaceId !== previousWorkspaceId) {
      if (previousWorkspaceId) await stopDesktopIfActive(previousWorkspaceId)
      error.value = null
      desktopStore.reset()
    }
  },
)

watch(
  () => desktopStore.proxyUrl,
  () => {
    iframeLoaded.value = false
  },
)

watch(surfaceRef, () => {
  observeSurface()
})

watch(desktopIframeRef, () => {
  bindDesktopIframeKeydownListener()
})
</script>

<template>
  <!-- Offscreen fallback host: keeps the iframe alive when no visible host exists -->
  <div
    ref="hiddenHostRef"
    class="fixed top-0 -left-[10000px] h-24 w-32 overflow-hidden"
    aria-hidden="true"
  ></div>

  <Teleport v-if="teleportTarget" :to="teleportTarget">
    <div
      v-if="desktopStore.isConnected && desktopStore.proxyUrl"
      ref="surfaceRef"
      class="absolute inset-0"
      data-testid="desktop-surface"
    >
      <div
        class="flex h-full w-full items-center justify-center overflow-hidden"
        :class="{ 'p-2': !desktopStore.isOpen }"
      >
        <div
          class="shrink-0 overflow-hidden rounded-[var(--radius-xs)] border border-border bg-black shadow-sm"
          :style="scaledFrameStyle"
        >
          <iframe
            ref="desktopIframeRef"
            :src="desktopIframeSrc"
            title="Desktop"
            class="block border-0 transition-opacity duration-200"
            :class="{ 'opacity-0': !iframeLoaded }"
            :style="scaledIframeStyle"
            sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
            allow="clipboard-read; clipboard-write"
            @load="handleIframeLoad"
          />
        </div>
      </div>

      <!-- Themed placeholder until the KasmVNC page has loaded -->
      <div
        v-if="!iframeLoaded"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-card"
        data-testid="desktop-surface-loading"
      >
        <LoadingSpinner :size="24" />
        <span class="text-sm text-muted-foreground">Connecting to desktop…</span>
      </div>

      <div
        v-if="desktopStore.computerUseActive"
        class="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-black/55 px-4 text-center"
        tabindex="0"
        @keydown.prevent
      >
        <MousePointerClick :size="28" class="text-white" />
        <p class="max-w-md text-sm text-white">
          Computer-use is controlling this desktop. Watching is read-only.
          Taking control aborts the computer-use agent.
        </p>
        <Button size="sm" :disabled="takeControlBusy" @click="takeControl">
          Take control
        </Button>
      </div>
    </div>
  </Teleport>
</template>
