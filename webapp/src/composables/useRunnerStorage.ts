import { ref, watch, onMounted, onUnmounted, type Ref } from 'vue'
import { usePolling } from './usePolling'
import { formatStorageBytes } from '@opencuria/chat-core/workspaceDisk'
import * as api from '@/services/runnerStorage.api'
import type { RunnerStorage, DeletionRequest } from '@/types/runnerStorage'
export function useRunnerStorage(runnerId: Ref<string>) {
  const data = ref<RunnerStorage | null>(null)
  const deletions = ref<DeletionRequest[]>([])
  const error = ref('')
  const loading = ref(false)
  const refreshPending = ref(false)
  let requestedAt = ''
  let version = 0
  let active = true
  async function load() {
    const id = runnerId.value,
      token = ++version
    loading.value = true
    try {
      const [snapshot, requests] = await Promise.all([
        api.getRunnerStorage(id),
        api.listDeletions(),
      ])
      if (!active || token !== version || id !== runnerId.value) return
      data.value = snapshot
      deletions.value = requests.filter(
        (r) =>
          r.approval.images.some((i) => i.runner_id === id) ||
          r.approval.workspaces.some((w) => w.runner_id === id),
      )
      error.value = ''
      if (
        refreshPending.value &&
        snapshot.latest_complete &&
        snapshot.runtimes.length &&
        snapshot.runtimes.every((r) => r.fresh && r.received_at && r.received_at > requestedAt)
      )
        refreshPending.value = false
    } catch (e) {
      if (active && token === version)
        error.value = e instanceof Error ? e.message : 'Storage inspection failed'
    } finally {
      if (active && token === version) loading.value = false
    }
  }
  async function refresh() {
    const id = runnerId.value
    try {
      const result = await api.refreshRunnerStorage(id)
      if (!active || id !== runnerId.value) return
      requestedAt = result.requested_at
      refreshPending.value = true
      await load()
    } catch (e) {
      if (active && id === runnerId.value)
        error.value = e instanceof Error ? e.message : 'Scan request failed'
    }
  }
  const polling = usePolling(load, 5000)
  watch(runnerId, () => {
    version++
    data.value = null
    deletions.value = []
    error.value = ''
    refreshPending.value = false
    void load()
  })
  onMounted(polling.start)
  onUnmounted(() => {
    active = false
    version++
  })
  return { data, deletions, error, loading, refreshPending, load, refresh }
}
/** Binary-unit size shared with the mobile app ("12.4 GiB"). */
export const storageBytes = formatStorageBytes
