<script setup lang="ts">
/**
 * SidePanelDesktop — interactive live desktop inside the side panel.
 *
 * The session auto-starts when the desktop tab is first opened. The
 * KasmVNC iframe itself lives in DesktopSurface and is teleported into
 * the host below, so it is fully interactive right here and is never
 * reloaded when the desktop modal opens or closes.
 */
import { onMounted, toRef } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useDesktopSession } from '@/composables/useDesktopSession'
import { sidebarDesktopHost } from '@/lib/desktopSurfaceHost'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Monitor, Maximize2, RefreshCw, Square } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const desktopStore = useDesktopStore()
const {
  error,
  startDesktop,
  stopDesktop,
  handleReconnect,
} = useDesktopSession(toRef(props, 'workspaceId'))

onMounted(() => {
  if (!desktopStore.isConnected && !desktopStore.isConnecting) {
    void startDesktop()
  }
})

function handleMaximize(): void {
  desktopStore.open()
}

function setSidebarHost(el: Element | ComponentPublicInstance | null): void {
  sidebarDesktopHost.value = el instanceof HTMLElement ? el : null
}
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
    <div class="relative min-h-0 flex-1">
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

      <!-- Host for the persistent DesktopSurface iframe (interactive) -->
      <div
        v-else
        :ref="setSidebarHost"
        class="absolute inset-0"
        data-testid="side-panel-desktop-host"
      />
    </div>
  </div>
</template>
