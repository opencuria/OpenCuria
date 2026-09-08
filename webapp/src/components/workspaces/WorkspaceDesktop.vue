<script setup lang="ts">
/**
 * WorkspaceDesktop — the desktop modal.
 *
 * A large aspect-ratio-matched dialog (same pattern as the settings
 * sheet) that hosts the persistent DesktopSurface iframe. Closing the
 * modal only closes the view — the session keeps running and the live
 * stream stays visible in the side panel. There is no chat sidebar here
 * anymore.
 */
import { computed, toRef } from 'vue'
import type { CSSProperties } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useDesktopSession } from '@/composables/useDesktopSession'
import { desktopModalWidthCss, workspaceDesktopSize } from '@/lib/desktopGeometry'
import { modalDesktopHost } from '@/lib/desktopSurfaceHost'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Monitor, RefreshCw, Square, Copy, ClipboardPaste, X } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const desktopStore = useDesktopStore()
const workspaceStore = useWorkspaceStore()
const {
  error,
  clipboardBusy,
  startDesktop,
  stopDesktop,
  handleReconnect,
  copyFromVmClipboard,
  pasteToVmClipboard,
} = useDesktopSession(toRef(props, 'workspaceId'))

const desktopSize = computed(() => {
  const workspace =
    workspaceStore.workspaces.find((entry) => entry.id === props.workspaceId)
    ?? (workspaceStore.activeWorkspace?.id === props.workspaceId
      ? workspaceStore.activeWorkspace
      : null)
  return workspaceDesktopSize(workspace)
})

// Largest width that keeps the dialog (header + aspect-ratio viewport)
// inside the viewport with a minimal margin.
const contentStyle = computed<CSSProperties>(() => ({
  width: desktopModalWidthCss(desktopSize.value.width, desktopSize.value.height),
}))

function handleUpdateOpen(open: boolean): void {
  if (!open) desktopStore.close()
}

function setModalHost(el: Element | ComponentPublicInstance | null): void {
  modalDesktopHost.value = el instanceof HTMLElement ? el : null
}
</script>

<template>
  <Dialog :open="desktopStore.isOpen" @update:open="handleUpdateOpen">
    <!-- Width comes from the inline style (desktop aspect ratio); the
      important modifier only drops Dialog's default sm:max-w-md cap. -->
    <DialogContent
      :show-close-button="false"
      aria-describedby="workspace-desktop-description"
      class="gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-none!"
      :style="contentStyle"
      data-testid="workspace-desktop-modal"
      @open-auto-focus.prevent
    >
      <DialogTitle class="sr-only">Desktop</DialogTitle>
      <DialogDescription id="workspace-desktop-description" class="sr-only">
        Interactive remote desktop session.
      </DialogDescription>

      <!-- Header -->
      <div class="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
        <div class="flex items-center gap-2">
          <Monitor :size="14" class="shrink-0 text-muted-foreground" />
          <span class="text-xs font-medium text-foreground">Desktop</span>
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
            variant="ghost"
            size="icon-sm"
            class="h-6 w-6 opacity-50 hover:opacity-100"
            :disabled="!desktopStore.isConnected || clipboardBusy || desktopStore.computerUseActive"
            title="Copy VM clipboard to local clipboard"
            @click="copyFromVmClipboard"
          >
            <Copy :size="11" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-6 w-6 opacity-50 hover:opacity-100"
            :disabled="!desktopStore.isConnected || clipboardBusy || desktopStore.computerUseActive"
            title="Paste local clipboard into VM clipboard"
            @click="pasteToVmClipboard"
          >
            <ClipboardPaste :size="11" />
          </Button>
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
            v-if="desktopStore.isConnected || desktopStore.isConnecting"
            variant="ghost"
            size="icon-sm"
            class="h-6 w-6"
            title="Stop desktop session"
            data-testid="desktop-modal-stop"
            @click="stopDesktop"
          >
            <Square :size="12" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-6 w-6"
            title="Close desktop"
            data-testid="desktop-modal-close"
            @click="desktopStore.close()"
          >
            <X :size="12" />
          </Button>
        </div>
      </div>

      <!-- Viewport, aspect-ratio matched to the desktop resolution -->
      <div
        class="relative w-full bg-black"
        :style="{ aspectRatio: `${desktopSize.width} / ${desktopSize.height}` }"
        data-testid="desktop-viewport"
      >
        <div
          v-if="desktopStore.isConnecting"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-card"
        >
          <LoadingSpinner :size="24" />
          <span class="text-sm text-muted-foreground">Starting desktop session…</span>
        </div>

        <div
          v-else-if="error"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-card px-4"
        >
          <p class="text-sm text-destructive text-center">{{ error }}</p>
          <Button size="sm" @click="startDesktop">
            Retry
          </Button>
        </div>

        <div
          v-else-if="!desktopStore.isConnected"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-card"
        >
          <Monitor :size="32" class="text-muted-foreground" />
          <p class="text-sm text-muted-foreground">Desktop session not active</p>
          <Button size="sm" data-testid="desktop-modal-start" @click="startDesktop">
            <Monitor :size="14" class="mr-1" />
            Start Desktop
          </Button>
        </div>

        <!-- Host for the persistent DesktopSurface iframe -->
        <div
          v-else
          :ref="setModalHost"
          class="absolute inset-0"
          data-testid="desktop-modal-host"
        />
      </div>
    </DialogContent>
  </Dialog>
</template>
