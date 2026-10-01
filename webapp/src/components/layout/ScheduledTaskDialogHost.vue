<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onReconnect } from '@/services/socket'
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

async function fetchTasks(): Promise<void> {
  await taskStore.refresh()
}
const { start, stop } = usePolling(fetchTasks, 60_000)
const cleanupReconnect = onReconnect(() => {
  void fetchTasks().catch(() => undefined)
})
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
})
</script>

<template>
  <ScheduledTaskDialog v-if="open" :open="open" @update:open="handleOpenChange" />
</template>
