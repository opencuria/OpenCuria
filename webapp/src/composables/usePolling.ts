import { ref, onUnmounted, watch, type Ref, type MaybeRef, toValue } from 'vue'

/**
 * Composable for periodic data fetching.
 *
 * Calls `fetchFn` immediately and then every `intervalMs` milliseconds.
 * `intervalMs` can be a reactive ref/computed — the timer is restarted
 * automatically when it changes.
 *
 * Automatically stops when the component is unmounted.
 */
export function usePolling(
  fetchFn: () => Promise<void>,
  intervalMs: MaybeRef<number> = 5000,
): {
  isPolling: Ref<boolean>
  start: () => void
  stop: () => void
  restart: () => void
} {
  const isPolling = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null

  function clearTimer(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function start(): void {
    if (isPolling.value) return
    isPolling.value = true
    fetchFn()
    timer = setInterval(fetchFn, toValue(intervalMs))
  }

  function stop(): void {
    isPolling.value = false
    clearTimer()
  }

  function restart(): void {
    clearTimer()
    if (isPolling.value) {
      timer = setInterval(fetchFn, toValue(intervalMs))
    }
  }

  // Restart timer when interval changes (e.g. while image artifacts are being created)
  watch(
    () => toValue(intervalMs),
    () => {
      if (isPolling.value) restart()
    },
  )

  onUnmounted(stop)

  return { isPolling, start, stop, restart }
}
