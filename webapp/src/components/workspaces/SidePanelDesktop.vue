<script setup lang="ts">
/**
 * SidePanelDesktop — embedded live desktop preview inside the side panel.
 *
 * Renders the KasmVNC stream scaled to fit the panel. The embedded view is
 * a non-interactive preview: clicking it (or the maximize button) opens the
 * fullscreen desktop overlay where interaction happens.
 */
import { ref, computed, onMounted, onBeforeUnmount, toRef } from 'vue'
import type { CSSProperties } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useDesktopSession } from '@/composables/useDesktopSession'
import { getConfig } from '@/services/config'
import { desktopIframeSrc as buildDesktopIframeSrc, workspaceDesktopSize } from '@/lib/desktopGeometry'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Monitor, Maximize2, RefreshCw, Square, MousePointerClick } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const desktopStore = useDesktopStore()
const workspaceStore = useWorkspaceStore()
const {
  error,
  takeControlBusy,
  startDesktop,
  stopDesktop,
  stopDesktopIfActive,
  handleReconnect,
  takeControl,
  setupSocketListeners,
  cleanupSocketListeners,
} = useDesktopSession(toRef(props, 'workspaceId'))

const viewportHostRef = ref<HTMLElement | null>(null)
const viewportWidth = ref(0)
const viewportHeight = ref(0)
let resizeObserver: ResizeObserver | null = null

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

function handleMaximize(): void {
  desktopStore.open()
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
  setupSocketListeners()
  observeViewportHost()
})

onBeforeUnmount(() => {
  // The fullscreen overlay owns the session lifecycle while it is open.
  if (!desktopStore.isOpen) {
    void stopDesktopIfActive(props.workspaceId)
  }
  cleanupSocketListeners()
  resizeObserver?.disconnect()
  resizeObserver = null
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col" data-testid="side-panel-desktop">
    <!-- Toolbar -->
    <div class="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
      <div class="flex items-center gap-2">
        <span class="text-xs font-medium text-foreground">Live view</span>
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
        <Button
          v-if="desktopStore.isConnected"
          variant="ghost"
          size="icon-sm"
          class="h-6 w-6"
          title="Reconnect desktop"
          @click="handleReconnect"
        >
          <RefreshCw :size="12" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="h-6 w-6"
          title="Maximize desktop"
          :disabled="!desktopStore.isConnected"
          data-testid="side-panel-desktop-maximize"
          @click="handleMaximize"
        >
          <Maximize2 :size="12" />
        </Button>
        <Button
          v-if="desktopStore.isConnected || desktopStore.isConnecting"
          variant="ghost"
          size="icon-sm"
          class="h-6 w-6"
          title="Stop desktop session"
          data-testid="side-panel-desktop-stop"
          @click="stopDesktop"
        >
          <Square :size="12" />
        </Button>
      </div>
    </div>

    <!-- Viewport -->
    <div ref="viewportHostRef" class="relative min-h-0 flex-1">
      <div
        v-if="desktopStore.isConnecting"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3"
      >
        <LoadingSpinner :size="24" />
        <span class="text-sm text-muted-foreground">Starting desktop session…</span>
      </div>

      <div
        v-else-if="error"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 px-4"
      >
        <p class="text-center text-sm text-destructive">{{ error }}</p>
        <Button size="sm" @click="startDesktop">
          Retry
        </Button>
      </div>

      <div
        v-else-if="!desktopStore.isConnected"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3"
      >
        <Monitor :size="32" class="text-muted-foreground" />
        <p class="text-sm text-muted-foreground">Desktop session not active</p>
        <Button size="sm" data-testid="side-panel-desktop-start" @click="startDesktop">
          <Monitor :size="14" class="mr-1" />
          Start Desktop
        </Button>
      </div>

      <div v-else-if="desktopStore.proxyUrl" class="absolute inset-0">
        <div
          class="flex h-full w-full cursor-pointer items-center justify-center overflow-hidden p-2"
          title="Maximize desktop"
          @click="handleMaximize"
        >
          <div
            class="pointer-events-none shrink-0 overflow-hidden rounded-[var(--radius-xs)] border border-border bg-black shadow-sm"
            :style="scaledFrameStyle"
          >
            <iframe
              :src="desktopIframeSrc"
              class="block border-0"
              :style="scaledIframeStyle"
              sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
              tabindex="-1"
            />
          </div>
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
    </div>
  </div>
</template>
