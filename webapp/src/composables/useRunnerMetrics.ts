import { computed, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { getRunnerMetricsHistory, getRunnerMetricsLatest } from '@/services/runners.api'
import type { RunnerSystemMetrics } from '@/types'
import { usePolling } from './usePolling'

export const RUNNER_METRICS_STALE_MS = 180_000

/** Unknown or invalid denominators must never look like healthy zero usage. */
export function resourcePercent(used: number | null | undefined, total = 100): number | null {
  if (used == null || used < 0 || !Number.isFinite(used) || !Number.isFinite(total) || total <= 0)
    return null
  const percent = (used / total) * 100
  return Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : null
}

export function sampleOutdated(timestamp: string | null | undefined, now: number): boolean {
  const time = timestamp ? Date.parse(timestamp) : NaN
  return !Number.isFinite(time) || time > now || now - time >= RUNNER_METRICS_STALE_MS
}

/** Fetch independent host snapshots and history, including last-known samples offline. */
export function useRunnerMetrics(runnerId: Ref<string>) {
  const metrics = ref<RunnerSystemMetrics | null>(null)
  const history = ref<RunnerSystemMetrics[]>([])
  const loading = ref(false)
  const latestError = ref('')
  const historyError = ref('')
  const error = computed(() => [latestError.value, historyError.value].filter(Boolean).join(' '))
  const now = ref(Date.now())
  let clock: ReturnType<typeof setInterval> | undefined
  let active = true
  let version = 0

  async function load(): Promise<void> {
    if (!active) return
    const id = runnerId.value
    const token = ++version
    const current = () => active && token === version && id === runnerId.value
    loading.value = true
    // Each request commits independently; history cannot withhold a usable latest sample.
    await Promise.all([
      getRunnerMetricsLatest(id).then(
        (sample) => {
          if (!current()) return
          metrics.value = sample
          latestError.value = ''
        },
        () => {
          if (current()) latestError.value = 'Host sample could not be refreshed.'
        },
      ),
      getRunnerMetricsHistory(id, 24).then(
        (samples) => {
          if (!current()) return
          history.value = samples
          historyError.value = ''
        },
        () => {
          if (current()) historyError.value = 'CPU history could not be refreshed.'
        },
      ),
    ])
    if (current()) loading.value = false
  }

  const polling = usePolling(load, 60_000)
  watch(runnerId, () => {
    version++
    metrics.value = null
    history.value = []
    latestError.value = ''
    historyError.value = ''
    void load()
  })
  onMounted(() => {
    clock = setInterval(() => {
      now.value = Date.now()
    }, 15_000)
    polling.start()
  })
  onUnmounted(() => {
    active = false
    version++
    clearInterval(clock)
  })
  return { metrics, history, loading, error, latestError, historyError, now, load }
}
