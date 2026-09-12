<script setup lang="ts">
/**
 * HarnessDesktopMini — lightweight LIVE status card for computer-use runs.
 *
 * Deliberately renders NO KasmVNC iframe: the single persistent
 * DesktopSurface iframe owns the only VNC client per workspace, and a
 * second parallel client doubles the UI.rfb race surface (stale restart
 * sockets, duplicate tunnel bindings). The card mirrors the global
 * desktop store status (Connecting/LIVE for this workspace) and opens
 * the full desktop on click, where the persistent surface is teleported
 * into the modal host.
 */
import { computed, inject, ref } from 'vue'

import { useDesktopStore } from '@/stores/desktop'
import { useWorkspaceStore } from '@/stores/workspaces'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { workspaceDesktopSize } from '@/lib/desktopGeometry'

const workspaceIdRef = inject(harnessWorkspaceIdKey, ref(''))
const workspaceId = computed(() => workspaceIdRef.value)

const desktopStore = useDesktopStore()
const workspaceStore = useWorkspaceStore()

const desktopSize = computed(() => {
  const workspace =
    workspaceStore.workspaces.find((entry) => entry.id === workspaceId.value)
    ?? (workspaceStore.activeWorkspace?.id === workspaceId.value
      ? workspaceStore.activeWorkspace
      : null)
  return workspaceDesktopSize(workspace)
})

const fullDesktopOpen = computed(() => desktopStore.isOpen)

// Only reflect sessions for this card's workspace; never another
// workspace's persistent surface.
const isOwnWorkspace = computed(
  () => desktopStore.workspaceId === workspaceId.value,
)
const isLive = computed(() => isOwnWorkspace.value && desktopStore.isConnected)
const isConnecting = computed(
  () => isOwnWorkspace.value && desktopStore.isConnecting,
)

function openFullDesktop(): void {
  desktopStore.open()
}
</script>

<template>
  <button
    v-if="!fullDesktopOpen"
    type="button"
    data-testid="harness-desktop-mini"
    class="mt-2 w-full overflow-hidden rounded-md border border-border bg-black text-left"
    @click.stop="openFullDesktop"
  >
    <div
      class="relative w-full overflow-hidden"
      :style="{ aspectRatio: `${desktopSize.width} / ${desktopSize.height}` }"
    >
      <span
        v-if="isLive"
        class="absolute left-1.5 top-1.5 z-10 rounded bg-red-600 px-1.5 py-px text-[9px] font-semibold tracking-wide text-white"
      >
        LIVE
      </span>
      <span
        v-else
        class="absolute left-1.5 top-1.5 z-10 rounded bg-muted px-1.5 py-px text-[9px] font-semibold tracking-wide text-muted-foreground"
      >
        DESKTOP
      </span>
      <div class="flex h-full items-center justify-center text-[11px] text-white/70">
        <span v-if="isLive">Open live desktop</span>
        <span v-else-if="isConnecting">Connecting to desktop…</span>
        <span v-else>Open desktop</span>
      </div>
    </div>
  </button>
</template>
