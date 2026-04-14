<script setup lang="ts">
/**
 * WorkspaceDesktop — KasmVNC desktop viewer panel.
 *
 * Starts a desktop session on demand, then displays the KasmVNC
 * web interface in an iframe. The backend reverse-proxies both
 * HTTP (static KasmVNC client files) and WebSocket (VNC data) at
 * /ws/desktop/{workspace_id}/.
 */

import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useDesktopStore } from '@/stores/desktop'
import * as workspacesApi from '@/services/workspaces.api'
import { onEvent } from '@/services/socket'
import { getConfig } from '@/services/config'
import { UiButton, UiSpinner } from '@/components/ui'
import { X, Monitor, RefreshCw, Minus } from 'lucide-vue-next'

const props = defineProps<{
  workspaceId: string
}>()

const desktopStore = useDesktopStore()

const error = ref<string | null>(null)
const cleanupFns: (() => void)[] = []

/**
 * Build the iframe src URL pointing to the backend proxy.
 * The proxy serves KasmVNC's web client files over HTTP and
 * proxies VNC WebSocket connections, all authenticated via
 * the JWT token query parameter.
 */
const desktopIframeSrc = computed(() => {
  if (!desktopStore.proxyUrl) return ''
  const token = localStorage.getItem('kern_access_token') || ''
  const config = getConfig()
  // In production wsBaseUrl is the backend origin (e.g. http://host:8000).
  // In dev mode it's empty and the Vite proxy handles /ws/desktop/.
  const base = config.wsBaseUrl || ''
  return `${base}${desktopStore.proxyUrl}?token=${encodeURIComponent(token)}`
})

async function startDesktop(): Promise<void> {
  if (
    desktopStore.workspaceId
    && desktopStore.workspaceId !== props.workspaceId
  ) {
    desktopStore.reset()
  }
  if (desktopStore.isConnecting || desktopStore.isConnected) return
  error.value = null
  desktopStore.setConnecting(props.workspaceId)

  try {
    // Check if a session is already running
    const status = await workspacesApi.getDesktopStatus(props.workspaceId)
    if (status.active && status.proxy_url) {
      desktopStore.setConnected(props.workspaceId, status.proxy_url)
      return
    }
    await workspacesApi.startDesktop(props.workspaceId)
    // The desktop:started event will arrive via Socket.IO
  } catch (err: unknown) {
    // 409 means desktop is already starting — poll status
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
    // Ignore stop errors during teardown; state is reset locally below.
  }
  desktopStore.setDisconnected()
}

async function handleClose(): Promise<void> {
  if (await stopDesktop()) {
    desktopStore.close()
  }
}

function handleMinimize(): void {
  desktopStore.minimize()
}

function handleReconnect(): void {
  desktopStore.setDisconnected()
  startDesktop()
}

// --- Socket.IO event handlers ---

onMounted(() => {
  if (desktopStore.workspaceId && desktopStore.workspaceId !== props.workspaceId) {
    desktopStore.reset()
  }

  const removeStarted = onEvent('desktop:started', (data) => {
    if (data.workspace_id !== props.workspaceId) return
    desktopStore.setConnected(props.workspaceId, data.proxy_url)
  })
  cleanupFns.push(removeStarted)

  const removeStopped = onEvent('desktop:stopped', (data) => {
    if (data.workspace_id !== props.workspaceId) return
    desktopStore.setDisconnected()
  })
  cleanupFns.push(removeStopped)

  const removeError = onEvent('workspace:error', (data) => {
    if (data.workspace_id !== props.workspaceId) return
    if (desktopStore.isConnecting) {
      error.value = data.error
      desktopStore.setDisconnected()
    }
  })
  cleanupFns.push(removeError)

  // Auto-start if panel is open but not connected
  if (desktopStore.isOpen && !desktopStore.isConnected && !desktopStore.isConnecting) {
    startDesktop()
  }
})

onBeforeUnmount(() => {
  void stopDesktopIfActive(props.workspaceId)
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0
})

// Auto-start when panel opens
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
      if (previousWorkspaceId) {
        await stopDesktopIfActive(previousWorkspaceId)
      }
      error.value = null
      desktopStore.reset()
    }
  },
)
</script>

<template>
  <div v-show="!desktopStore.isMinimized" class="fixed inset-0 z-[110] flex flex-col bg-surface">
    <!-- Header bar -->
    <div class="flex items-center justify-between px-3 py-1.5 border-b border-border bg-surface shrink-0">
      <div class="flex items-center gap-2">
        <Monitor :size="14" class="text-muted-fg" />
        <span class="text-xs font-medium text-fg">Desktop</span>
        <span
          v-if="desktopStore.isConnected"
          class="inline-block w-1.5 h-1.5 rounded-full bg-success"
          title="Connected"
        />
        <span
          v-else-if="desktopStore.isConnecting"
          class="inline-block w-1.5 h-1.5 rounded-full bg-warning animate-pulse"
          title="Connecting…"
        />
      </div>
      <div class="flex items-center gap-1">
        <UiButton
          variant="ghost"
          size="icon-sm"
          title="Minimize desktop panel"
          @click="handleMinimize"
        >
          <Minus :size="14" />
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
          title="Close desktop panel"
          @click="handleClose"
        >
          <X :size="14" />
        </UiButton>
      </div>
    </div>

    <!-- Content area -->
    <div class="flex-1 relative min-h-0">
      <!-- Connecting state -->
      <div
        v-if="desktopStore.isConnecting"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface"
      >
        <UiSpinner :size="24" />
        <span class="text-sm text-muted-fg">Starting desktop session…</span>
      </div>

      <!-- Error state -->
      <div
        v-else-if="error"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface px-4"
      >
        <p class="text-sm text-error text-center">{{ error }}</p>
        <UiButton size="sm" @click="startDesktop">
          Retry
        </UiButton>
      </div>

      <!-- Not started state -->
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

      <!-- Connected: KasmVNC iframe -->
      <iframe
        v-if="desktopStore.isConnected && desktopStore.proxyUrl"
        :src="desktopIframeSrc"
        class="w-full h-full border-0"
        sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
        allow="clipboard-read; clipboard-write"
      />
    </div>
  </div>
</template>
