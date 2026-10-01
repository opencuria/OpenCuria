import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import {
  createScheduledTask,
  deleteScheduledTask,
  listScheduledTasks,
  updateScheduledTask,
} from '@/services/scheduledTasks.api'
import type {
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskPatch,
} from '@/services/scheduledTasks.api'

export const useScheduledTaskStore = defineStore('scheduledTasks', () => {
  const authStore = useAuthStore()
  const tasks = ref<ScheduledTask[]>([])
  const selectedTaskId = ref<string | null>(null)
  const dialogOpen = ref(false)
  const loading = ref(false)
  const loadError = ref('')
  const organizationId = ref<string | null>(authStore.activeOrganizationId)
  let generation = 0
  let loadGeneration = 0
  let mutationGeneration = 0
  let pendingLoad: Promise<void> | null = null

  const selectedTask = computed(
    () => tasks.value.find((task) => task.id === selectedTaskId.value) ?? null,
  )

  function invalidatePendingLoad(): void {
    mutationGeneration += 1
    loadGeneration += 1
    pendingLoad = null
    loading.value = false
  }

  watch(
    () => authStore.activeOrganizationId,
    () => {
      syncOrganization()
    },
  )

  function syncOrganization(): number {
    const current = authStore.activeOrganizationId
    if (organizationId.value !== current) {
      organizationId.value = current
      generation += 1
      pendingLoad = null
      loadGeneration += 1
      tasks.value = []
      selectedTaskId.value = null
      dialogOpen.value = false
      loadError.value = ''
      loading.value = false
    }
    return generation
  }

  async function refresh(force = false): Promise<void> {
    const requestGeneration = syncOrganization()
    if (!organizationId.value) {
      tasks.value = []
      return
    }
    if (pendingLoad && !force) return pendingLoad
    if (force) loadGeneration += 1
    const requestMutationGeneration = mutationGeneration
    loading.value = true
    loadError.value = ''
    const requestId = ++loadGeneration
    const request = listScheduledTasks()
      .then((result) => {
        if (
          generation !== requestGeneration ||
          organizationId.value !== authStore.activeOrganizationId ||
          requestId !== loadGeneration
        )
          return
        if (requestMutationGeneration === mutationGeneration)
          tasks.value = [...result].sort((a, b) => a.next_run_at.localeCompare(b.next_run_at))
        else {
          const refreshed = new Map(result.map((task) => [task.id, task]))
          for (const task of tasks.value) refreshed.set(task.id, task)
          tasks.value = [...refreshed.values()].sort((a, b) =>
            a.next_run_at.localeCompare(b.next_run_at),
          )
        }
      })
      .catch((error: unknown) => {
        if (generation !== requestGeneration || requestId !== loadGeneration) return
        loadError.value = error instanceof Error ? error.message : 'Could not load scheduled tasks.'
        throw error
      })
      .finally(() => {
        if (generation === requestGeneration && requestId === loadGeneration) {
          loading.value = false
          pendingLoad = null
        }
      })
    pendingLoad = request
    return request
  }

  async function create(input: ScheduledTaskInput): Promise<ScheduledTask> {
    const requestGeneration = syncOrganization()
    invalidatePendingLoad()
    const task = await createScheduledTask(input)
    if (generation === requestGeneration && organizationId.value === authStore.activeOrganizationId) {
      invalidatePendingLoad()
      upsert(task)
    }
    return task
  }

  async function update(id: string, input: ScheduledTaskPatch): Promise<ScheduledTask> {
    const requestGeneration = syncOrganization()
    invalidatePendingLoad()
    const task = await updateScheduledTask(id, input)
    if (generation === requestGeneration && organizationId.value === authStore.activeOrganizationId) {
      invalidatePendingLoad()
      upsert(task)
    }
    return task
  }

  async function remove(id: string): Promise<void> {
    const requestGeneration = syncOrganization()
    invalidatePendingLoad()
    await deleteScheduledTask(id)
    if (generation !== requestGeneration || organizationId.value !== authStore.activeOrganizationId)
      return
    invalidatePendingLoad()
    tasks.value = tasks.value.filter((task) => task.id !== id)
    if (selectedTaskId.value === id) selectedTaskId.value = null
  }

  function upsert(task: ScheduledTask): void {
    tasks.value = [...tasks.value.filter((item) => item.id !== task.id), task].sort((a, b) =>
      a.next_run_at.localeCompare(b.next_run_at),
    )
  }

  function openNew(): void {
    syncOrganization()
    selectedTaskId.value = null
    dialogOpen.value = true
  }

  function openTask(id: string): void {
    syncOrganization()
    selectedTaskId.value = id
    dialogOpen.value = true
  }

  function closeDialog(): void {
    dialogOpen.value = false
  }

  return {
    tasks,
    selectedTaskId,
    selectedTask,
    dialogOpen,
    loading,
    loadError,
    refresh,
    create,
    update,
    remove,
    upsert,
    openNew,
    openTask,
    closeDialog,
  }
})
