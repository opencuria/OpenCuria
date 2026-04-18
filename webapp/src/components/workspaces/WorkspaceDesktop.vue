<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import type { CSSProperties } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useNotificationStore } from '@/stores/notifications'
import * as workspacesApi from '@/services/workspaces.api'
import { onEvent } from '@/services/socket'
import { getConfig } from '@/services/config'
import { UiButton, UiSelect, UiSpinner } from '@/components/ui'
import { X, Monitor, RefreshCw, Minus, RotateCw, Copy, ClipboardPaste } from 'lucide-vue-next'

const props = defineProps<{
  workspaceId: string
}>()

const desktopStore = useDesktopStore()
const notifications = useNotificationStore()
const error = ref<string | null>(null)
const clipboardBusy = ref(false)
const desktopIframeRef = ref<HTMLIFrameElement | null>(null)
const cleanupFns: (() => void)[] = []
const viewportHostRef = ref<HTMLElement | null>(null)
const viewportWidth = ref(0)
const viewportHeight = ref(0)
const viewportPreset = ref('auto')
const rotatePreset = ref(false)
let resizeObserver: ResizeObserver | null = null
let iframeKeydownCleanup: (() => void) | null = null
let isDispatchingSyntheticPasteShortcut = false

type ViewportPreset = {
  value: string
  label: string
  width: number
  height: number
}

const VIEWPORT_PRESETS: ViewportPreset[] = [
  { value: 'phone', label: 'Smartphone · 390×844', width: 390, height: 844 },
  { value: 'tablet', label: 'Tablet · 768×1024', width: 768, height: 1024 },
  { value: 'laptop', label: 'Laptop · 1366×768', width: 1366, height: 768 },
  { value: 'desktop', label: 'Desktop · 1920×1080', width: 1920, height: 1080 },
]

const viewportPresetOptions = computed(() => [
  { value: 'auto', label: 'Auto' },
  ...VIEWPORT_PRESETS.map((preset) => ({ value: preset.value, label: preset.label })),
])

const selectedPreset = computed(() =>
  VIEWPORT_PRESETS.find((preset) => preset.value === viewportPreset.value) ?? null,
)
const isCustomPreset = computed(() => selectedPreset.value !== null)

const presetWidth = computed(() => {
  if (!selectedPreset.value) return 0
  return rotatePreset.value ? selectedPreset.value.height : selectedPreset.value.width
})

const presetHeight = computed(() => {
  if (!selectedPreset.value) return 0
  return rotatePreset.value ? selectedPreset.value.width : selectedPreset.value.height
})

const viewportScale = computed(() => {
  if (!selectedPreset.value) return 1
  if (viewportWidth.value <= 0 || viewportHeight.value <= 0) return 1
  const fitScale = Math.min(
    viewportWidth.value / Math.max(presetWidth.value, 1),
    viewportHeight.value / Math.max(presetHeight.value, 1),
  )
  return Math.max(Math.min(fitScale, 1), 0.1)
})

const scaledFrameStyle = computed<CSSProperties>(() => {
  if (!selectedPreset.value) return {}
  return {
    width: `${presetWidth.value}px`,
    height: `${presetHeight.value}px`,
    transform: `scale(${viewportScale.value})`,
    transformOrigin: 'center center',
  }
})

const scaledIframeStyle = computed<CSSProperties>(() => {
  if (!selectedPreset.value) return {}
  return {
    width: `${presetWidth.value}px`,
    height: `${presetHeight.value}px`,
  }
})

const desktopIframeSrc = computed(() => {
  if (!desktopStore.proxyUrl) return ''
  const token = localStorage.getItem('kern_access_token') || ''
  const config = getConfig()
  const base = config.wsBaseUrl || ''
  return `${base}${desktopStore.proxyUrl}?token=${encodeURIComponent(token)}`
})

async function startDesktop(): Promise<void> {
  if (desktopStore.workspaceId && desktopStore.workspaceId !== props.workspaceId) {
    desktopStore.reset()
  }
  if (desktopStore.isConnecting || desktopStore.isConnected) return
  error.value = null
  desktopStore.setConnecting(props.workspaceId)

  try {
    const status = await workspacesApi.getDesktopStatus(props.workspaceId)
    if (status.active && status.proxy_url) {
      desktopStore.setConnected(props.workspaceId, status.proxy_url)
      return
    }
    await workspacesApi.startDesktop(props.workspaceId)
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    if (msg.includes('409') || msg.toLowerCase().includes('conflict')) {
      try {
        const status = await workspacesApi.getDesktopStatus(props.workspaceId)
        if (status.active && status.proxy_url) {
          desktopStore.setConnected(props.workspaceId, status.proxy_url)
          return
        }
      } catch { /* fall through */ }
    }
    error.value = msg
    desktopStore.setDisconnected()
  }
}

async function stopDesktop(): Promise<boolean> {
  try {
    await workspacesApi.stopDesktop(props.workspaceId)
    desktopStore.setDisconnected()
    return true
  } catch {
    error.value = 'Failed to stop desktop session'
    return false
  }
}

async function stopDesktopIfActive(targetWorkspaceId: string): Promise<void> {
  if (desktopStore.workspaceId !== targetWorkspaceId) return
  if (!desktopStore.isConnected && !desktopStore.isConnecting) return
  try {
    await workspacesApi.stopDesktop(targetWorkspaceId)
  } catch {
    // Ignore stop errors during teardown.
  }
  desktopStore.setDisconnected()
}

async function handleClose(): Promise<void> {
  if (await stopDesktop()) desktopStore.close()
}

function handleMinimize(): void {
  desktopStore.minimize()
}

function handleReconnect(): void {
  desktopStore.setDisconnected()
  startDesktop()
}

function toggleRotatePreset(): void {
  if (!isCustomPreset.value) return
  rotatePreset.value = !rotatePreset.value
}

async function copyFromVmClipboard(): Promise<boolean> {
  if (!desktopStore.isConnected || clipboardBusy.value) return false
  clipboardBusy.value = true
  try {
    const { text } = await workspacesApi.readDesktopClipboard(props.workspaceId)
    await navigator.clipboard.writeText(text || '')
    notifications.success('Copied from VM', 'VM clipboard copied to local clipboard.')
    return true
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    notifications.error('Copy failed', msg)
    return false
  } finally {
    clipboardBusy.value = false
  }
}

async function pasteToVmClipboard(): Promise<boolean> {
  if (!desktopStore.isConnected || clipboardBusy.value) return false
  clipboardBusy.value = true
  try {
    const text = await navigator.clipboard.readText()
    await workspacesApi.writeDesktopClipboard(props.workspaceId, text || '')
    notifications.success('Pasted to VM', 'Local clipboard sent to VM clipboard.')
    return true
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    notifications.error('Paste failed', msg)
    return false
  } finally {
    clipboardBusy.value = false
  }
}

function shouldHandleClipboardShortcut(event: KeyboardEvent): boolean {
  if (!desktopStore.isOpen || desktopStore.isMinimized || !desktopStore.isConnected) return false
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

function onGlobalKeydown(event: KeyboardEvent): void {
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

function observeViewportHost(): void {
  if (!viewportHostRef.value) return
  const refreshBounds = () => {
    if (!viewportHostRef.value) return
    const rect = viewportHostRef.value.getBoundingClientRect()
    viewportWidth.value = rect.width
    viewportHeight.value = rect.height
  }
  refreshBounds()
  resizeObserver = new ResizeObserver(refreshBounds)
  resizeObserver.observe(viewportHostRef.value)
}

onMounted(() => {
  if (desktopStore.workspaceId && desktopStore.workspaceId !== props.workspaceId) {
    desktopStore.reset()
  }

  cleanupFns.push(
    onEvent('desktop:started', (data) => {
      if (data.workspace_id !== props.workspaceId) return
      desktopStore.setConnected(props.workspaceId, data.proxy_url)
    }),
    onEvent('desktop:stopped', (data) => {
      if (data.workspace_id !== props.workspaceId) return
      desktopStore.setDisconnected()
    }),
    onEvent('workspace:error', (data) => {
      if (data.workspace_id !== props.workspaceId) return
      if (desktopStore.isConnecting) {
        error.value = data.error
        desktopStore.setDisconnected()
      }
    }),
  )

  if (desktopStore.isOpen && !desktopStore.isConnected && !desktopStore.isConnecting) {
    startDesktop()
  }
  observeViewportHost()
  window.addEventListener('keydown', onGlobalKeydown)
})

onBeforeUnmount(() => {
  const keepRunningInBackground = (
    desktopStore.isOpen
    && desktopStore.isMinimized
    && desktopStore.workspaceId === props.workspaceId
  )
  if (!keepRunningInBackground) {
    void stopDesktopIfActive(props.workspaceId)
  }
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0
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
      startDesktop()
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
      viewportPreset.value = 'auto'
      rotatePreset.value = false
    }
  },
)

watch(
  () => desktopIframeRef.value,
  () => {
    bindDesktopIframeKeydownListener()
  },
)

watch(
  () => viewportPreset.value,
  (nextPreset) => {
    if (nextPreset === 'auto') rotatePreset.value = false
  },
)
</script>

<template>
  <div v-show="!desktopStore.isMinimized" class="fixed inset-0 z-[110] bg-surface">
    <div class="flex h-full flex-col bg-surface sm:flex-row">
      <div ref="viewportHostRef" class="order-2 min-h-0 flex-1 sm:order-1 relative">
        <div
          v-if="desktopStore.isConnecting"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface"
        >
          <UiSpinner :size="24" />
          <span class="text-sm text-muted-fg">Starting desktop session…</span>
        </div>

        <div
          v-else-if="error"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface px-4"
        >
          <p class="text-sm text-error text-center">{{ error }}</p>
          <UiButton size="sm" @click="startDesktop">
            Retry
          </UiButton>
        </div>

        <div
          v-else-if="!desktopStore.isConnected && !desktopStore.isConnecting"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface"
        >
          <Monitor :size="32" class="text-muted-fg" />
          <p class="text-sm text-muted-fg">Desktop session not active</p>
          <UiButton size="sm" @click="startDesktop">
            <Monitor :size="14" class="mr-1" />
            Start Desktop
          </UiButton>
        </div>

        <div
          v-else-if="desktopStore.isConnected && desktopStore.proxyUrl"
          class="absolute inset-0"
        >
          <iframe
            v-if="viewportPreset === 'auto'"
            ref="desktopIframeRef"
            :src="desktopIframeSrc"
            class="h-full w-full border-0"
            sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
            allow="clipboard-read; clipboard-write"
            @load="bindDesktopIframeKeydownListener"
          />
          <div
            v-else
            class="flex h-full w-full items-center justify-center overflow-hidden p-2 sm:p-3"
          >
            <div
              class="shrink-0 overflow-hidden rounded-[var(--radius-xs)] border border-border bg-black shadow-[var(--glass-shadow-sm)]"
              :style="scaledFrameStyle"
            >
              <iframe
                ref="desktopIframeRef"
                :src="desktopIframeSrc"
                class="block border-0"
                :style="scaledIframeStyle"
                sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
                allow="clipboard-read; clipboard-write"
                @load="bindDesktopIframeKeydownListener"
              />
            </div>
          </div>
        </div>
      </div>

      <div
        class="order-1 flex shrink-0 flex-col gap-2 border-b border-border bg-surface px-3 py-1.5 sm:order-2 sm:w-80 sm:border-b-0 sm:border-l sm:py-2 min-h-0"
      >
        <div class="flex items-center justify-between gap-2">
          <div class="flex items-center gap-2">
            <Monitor :size="14" class="shrink-0 text-muted-fg" />
            <span class="text-xs font-medium text-fg">Desktop</span>
            <span
              v-if="desktopStore.isConnected"
              class="inline-block h-1.5 w-1.5 rounded-full bg-success"
              title="Connected"
            />
            <span
              v-else-if="desktopStore.isConnecting"
              class="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning"
              title="Connecting…"
            />
          </div>
          <div class="flex items-center gap-1">
            <slot name="header-actions" />
            <UiButton
              variant="ghost"
              size="icon-sm"
              class="h-6 w-6 opacity-50 hover:opacity-100"
              :disabled="!desktopStore.isConnected || clipboardBusy"
              title="Copy VM clipboard to local clipboard"
              @click="copyFromVmClipboard"
            >
              <Copy :size="11" />
            </UiButton>
            <UiButton
              variant="ghost"
              size="icon-sm"
              class="h-6 w-6 opacity-50 hover:opacity-100"
              :disabled="!desktopStore.isConnected || clipboardBusy"
              title="Paste local clipboard into VM clipboard"
              @click="pasteToVmClipboard"
            >
              <ClipboardPaste :size="11" />
            </UiButton>
            <UiButton
              v-if="desktopStore.isConnected"
              variant="ghost"
              size="icon-sm"
              title="Reconnect desktop"
              @click="handleReconnect"
            >
              <RefreshCw :size="12" />
            </UiButton>
            <UiButton
              variant="ghost"
              size="icon-sm"
              title="Minimize desktop panel"
              @click="handleMinimize"
            >
              <Minus :size="12" />
            </UiButton>
            <UiButton
              variant="ghost"
              size="icon-sm"
              title="Close desktop panel"
              @click="handleClose"
            >
              <X :size="12" />
            </UiButton>
          </div>
        </div>

        <div class="text-[11px] text-muted-fg">Screen size</div>
        <UiSelect
          v-model="viewportPreset"
          :options="viewportPresetOptions"
          class="h-8 py-1 text-xs sm:h-9"
        />
        <UiButton
          variant="outline"
          size="sm"
          :disabled="!isCustomPreset"
          title="Rotate viewport preset"
          @click="toggleRotatePreset"
        >
          <RotateCw :size="12" class="mr-1" />
          Rotate
        </UiButton>

        <div class="hidden min-h-0 flex-1 overflow-hidden pt-2 lg:flex">
          <slot name="sidebar-content" />
        </div>
      </div>
    </div>
  </div>
</template>
