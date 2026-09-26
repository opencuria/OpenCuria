import { ref, onUnmounted, watch, type Ref, type MaybeRef, toValue } from 'vue'

/**
 * Composable for periodic data fetching.
 *
 * Starts with one immediate fetch, then waits `intervalMs` after each fetch
 * settles before starting the next one. This avoids overlapping requests and
 * interval callbacks piling up behind a slow request. Polling pauses while the
 * page is hidden and performs one fresh fetch when it becomes visible again.
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
  let timer: ReturnType<typeof setTimeout> | null = null
  let inFlight = false
  let runImmediatelyWhenFree = false
  let wasHidden = !pageIsVisible()
  let visibilityListener: (() => void) | null = null

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function pageIsVisible(): boolean {
    return typeof document === 'undefined' || document.visibilityState !== 'hidden'
  }

  function scheduleNext(): void {
    clearTimer()
    if (!isPolling.value || !pageIsVisible() || inFlight) return
    const delay = toValue(intervalMs)
    timer = setTimeout(() => {
      timer = null
      void runFetch()
    }, Number.isFinite(delay) ? Math.max(0, delay) : 5000)
  }

  async function runFetch(): Promise<void> {
    if (!isPolling.value || !pageIsVisible()) return
    if (inFlight) {
      runImmediatelyWhenFree = true
      return
    }
    clearTimer()
    inFlight = true
    try {
      await fetchFn()
    } catch (error) {
      // A failed poll must not create an unhandled rejection or permanently
      // stop future polls. The caller owns presentation/logging of failures.
      console.error('[polling] fetch failed:', error)
    } finally {
      inFlight = false
      if (!isPolling.value) {
        runImmediatelyWhenFree = false
      } else if (!pageIsVisible()) {
        runImmediatelyWhenFree = false
      } else if (runImmediatelyWhenFree) {
        runImmediatelyWhenFree = false
        void runFetch()
      } else {
        scheduleNext()
      }
    }
  }

  function onVisibilityChange(): void {
    if (!isPolling.value) return
    if (!pageIsVisible()) {
      wasHidden = true
      clearTimer()
    } else if (wasHidden) {
      wasHidden = false
      // Returning to the page refreshes immediately instead of waiting for a
      // possibly stale interval deadline.
      void runFetch()
    }
  }

  function start(): void {
    if (isPolling.value) return
    isPolling.value = true
    wasHidden = !pageIsVisible()
    if (typeof document !== 'undefined') {
      visibilityListener = onVisibilityChange
      document.addEventListener('visibilitychange', visibilityListener)
    }
    if (pageIsVisible()) void runFetch()
  }

  function stop(): void {
    isPolling.value = false
    runImmediatelyWhenFree = false
    clearTimer()
    if (visibilityListener !== null && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', visibilityListener)
      visibilityListener = null
    }
  }

  /** Restart the interval without forcing an extra immediate fetch. */
  function restart(): void {
    if (isPolling.value) scheduleNext()
  }

  watch(
    () => toValue(intervalMs),
    () => {
      if (isPolling.value) restart()
    },
  )

  onUnmounted(stop)

  return { isPolling, start, stop, restart }
}
