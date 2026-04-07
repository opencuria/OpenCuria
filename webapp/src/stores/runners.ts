/**
 * Runner Pinia store.
 *
 * Manages runner state with REST API integration and
 * Socket.IO real-time updates.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import type { Runner, RunnerCreateOut, RunnerUpdateIn } from '@/types'
import { RunnerStatus } from '@/types'
import * as runnersApi from '@/services/runners.api'
import { useNotificationStore } from './notifications'

export const useRunnerStore = defineStore('runners', () => {
  // --- State ---
  const runners = ref<Runner[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // --- Getters ---
  const onlineRunners = computed(() =>
    runners.value.filter((r) => r.status === RunnerStatus.ONLINE),
  )

  const offlineRunners = computed(() =>
    runners.value.filter((r) => r.status === RunnerStatus.OFFLINE),
  )

  function runnerById(id: string): Runner | undefined {
    return runners.value.find((r) => r.id === id)
  }

  // --- Actions ---

  async function fetchRunners(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      runners.value = await runnersApi.listRunners()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load runners'
    } finally {
      loading.value = false
    }
  }

  async function createRunner(name: string): Promise<RunnerCreateOut | null> {
    const notifications = useNotificationStore()
    try {
      const result = await runnersApi.createRunner({ name })
      // Refresh runners list
      await fetchRunners()
      notifications.success('Runner registered', `Runner "${result.name || result.id}" created successfully.`)
      return result
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create runner'
      notifications.error('Registration failed', msg)
      return null
    }
  }

  async function updateRunner(id: string, data: RunnerUpdateIn): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const updated = await runnersApi.updateRunner(id, data)
      const index = runners.value.findIndex((r) => r.id === id)
      if (index !== -1) runners.value[index] = updated
      notifications.success('Runner updated', 'QEMU limits and defaults were saved.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update runner'
      notifications.error('Update failed', msg)
      return false
    }
  }

  // --- Socket.IO integration ---

  function updateRunnerStatus(runnerId: string, status: RunnerStatus): void {
    const runner = runners.value.find((r) => r.id === runnerId)
    if (runner) {
      runner.status = status
    }
  }

  return {
    // State
    runners,
    loading,
    error,
    // Getters
    onlineRunners,
    offlineRunners,
    runnerById,
    // Actions
    fetchRunners,
    createRunner,
    updateRunner,
    updateRunnerStatus,
  }
})
