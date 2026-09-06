<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'

import { useDesktopStore } from '@/stores/desktop'
import { getDesktopStatus } from '@/services/workspaces.api'
import { getConfig } from '@/services/config'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'

const POLL_MS = 1000

const workspaceIdRef = inject(harnessWorkspaceIdKey, ref(''))
const workspaceId = computed(() => workspaceIdRef.value)

const desktopStore = useDesktopStore()

const proxyUrl = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

const fullDesktopOpen = computed(
  () => desktopStore.isOpen && !desktopStore.isMinimized,
)

const iframeSrc = computed(() => {
  if (!proxyUrl.value) return ''
  const token = localStorage.getItem('kern_access_token') || ''
  const base = getConfig().wsBaseUrl || ''
  return `${base}${proxyUrl.value}?token=${encodeURIComponent(token)}`
})

function stopPolling(): void {
  if (pollTimer == null) return
  clearInterval(pollTimer)
  pollTimer = null
}

async function refreshProxyUrl(): Promise<void> {
  if (
    desktopStore.proxyUrl &&
    desktopStore.workspaceId === workspaceId.value
  ) {
    proxyUrl.value = desktopStore.proxyUrl
    stopPolling()
    return
  }
  if (!workspaceId.value) return
  try {
    const status = await getDesktopStatus(workspaceId.value)
    if (status.active && status.proxy_url) {
      proxyUrl.value = status.proxy_url
      stopPolling()
    }
  } catch {
    /* keep polling while the computer-use hold starts Xvnc */
  }
}

function startPolling(): void {
  stopPolling()
  void refreshProxyUrl()
  pollTimer = setInterval(() => {
    void refreshProxyUrl()
  }, POLL_MS)
}

watch(
  () => [workspaceId.value, desktopStore.proxyUrl, fullDesktopOpen.value] as const,
  () => {
    if (fullDesktopOpen.value) {
      stopPolling()
      return
    }
    startPolling()
  },
)

onMounted(() => {
  if (!fullDesktopOpen.value) startPolling()
})

onUnmounted(() => {
  stopPolling()
})

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
    <div class="relative aspect-video w-full overflow-hidden">
      <span
        class="absolute left-1.5 top-1.5 z-10 rounded bg-red-600 px-1.5 py-px text-[9px] font-semibold tracking-wide text-white"
      >
        LIVE
      </span>
      <iframe
        v-if="iframeSrc"
        :src="iframeSrc"
        title="Computer-use desktop preview"
        class="pointer-events-none absolute inset-0 h-full w-full border-0"
        sandbox="allow-scripts allow-same-origin"
      />
      <div
        v-else
        class="flex h-full items-center justify-center text-[11px] text-white/70"
      >
        Connecting to desktop…
      </div>
    </div>
  </button>
</template>
