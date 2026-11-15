<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onEvent, onReconnect } from '@/services/socket'
import { usePolling } from '@/composables/usePolling'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import { useAuthStore } from '@/stores/auth'
const ScheduledTaskDialog = defineAsyncComponent(() => import('./ScheduledTaskDialog.vue'))

const route = useRoute()
const router = useRouter()
const taskStore = useScheduledTaskStore()
const authStore = useAuthStore()
let lastHandledQuery: string | null = null
let stripping = false
let opener: HTMLElement | null = null
let disposed = false
let refreshTimer: ReturnType<typeof setTimeout> | null = null

async function fetchTasks(): Promise<void> {
  await taskStore.refresh()
}
const { start, stop } = usePolling(fetchTasks, 15_000)
const cleanupReconnect = onReconnect(() => {
  void fetchTasks().catch(() => undefined)
})
// Session events precede some ledger writes. Coalesce bursts and let the
// dispatch settle before reading its state; polling repairs missed events.
function scheduleRefresh(): void {
  if (refreshTimer !== null) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void taskStore.refresh(true).catch(() => undefined)
  }, 250)
}
const cleanupStatus = onEvent('harness.session_status', scheduleRefresh)
const cleanupConversations = onEvent('harness.conversations_changed', scheduleRefresh)
const open = computed(() => taskStore.dialogOpen)

async function openFromQuery(): Promise<void> {
  const raw = route.query.scheduledTask
  if (typeof raw !== 'string' || !raw || stripping || raw === lastHandledQuery) return
  lastHandledQuery = raw
  const organizationId = authStore.activeOrganizationId
  if (raw === 'new') taskStore.openNew()
  else {
    let loadFailed = false
    if (!taskStore.tasks.length) {
      try {
        await taskStore.refresh()
      } catch {
        loadFailed = true
      }
    }
    if (
      disposed ||
      route.query.scheduledTask !== raw ||
      organizationId !== authStore.activeOrganizationId
    )
      return
    const taskId = raw === 'latest' ? taskStore.tasks[0]?.id : raw
    if (taskId) taskStore.openTask(taskId)
    else if (loadFailed) taskStore.openTask(raw)
    else {
      taskStore.loadError = 'No scheduled tasks are available.'
      taskStore.openTask('latest')
    }
  }
  if (disposed || route.query.scheduledTask !== raw) return
  await nextTick()
  if (disposed || route.query.scheduledTask !== raw) return
  const query = { ...route.query }
  delete query.scheduledTask
  stripping = true
  void router
    .replace({ path: route.path, query })
    .catch(() => undefined)
    .finally(() => {
      stripping = false
    })
}

watch(
  () => route.query.scheduledTask,
  (value) => {
    if (typeof value === 'string' && value) openFromQuery()
    else lastHandledQuery = null
  },
)
onMounted(() => {
  start()
  openFromQuery()
})

function handleOpenChange(value: boolean): void {
  if (value) {
    taskStore.dialogOpen = true
    return
  }
  taskStore.closeDialog()
}

watch(open, async (isOpen) => {
  if (isOpen) {
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    return
  }
  const target = opener
  opener = null
  if (!target) return
  await nextTick()
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      if (target.isConnected) target.focus()
    }),
  )
})

onUnmounted(() => {
  disposed = true
  stop()
  cleanupReconnect()
  cleanupStatus()
  cleanupConversations()
  if (refreshTimer !== null) clearTimeout(refreshTimer)
})
</script>

<template>
  <ScheduledTaskDialog v-if="open" :open="open" @update:open="handleOpenChange" />
</template>
