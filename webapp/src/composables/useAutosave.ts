import {
  computed,
  inject,
  onBeforeUnmount,
  provide,
  reactive,
  ref,
  watch,
  type InjectionKey,
} from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'

export interface AutosaveState {
  saving: boolean
  error: string | null
  dirty: boolean
  valid: boolean
  flush: () => Promise<boolean>
}

const autosaveRegistry: InjectionKey<Set<AutosaveState>> = Symbol('autosaveRegistry')

/** Keep settings open when a pending change cannot be persisted. */
export function provideAutosaveBoundary(): () => Promise<boolean> {
  const entries = new Set<AutosaveState>()
  provide(autosaveRegistry, entries)
  return async () => (await Promise.all([...entries].map((entry) => entry.flush()))).every(Boolean)
}

/** Debounce edits, serialize writes, and flush pending changes before leaving. */
export function useAutosave(options: {
  source: () => unknown
  dirty: () => boolean
  save: (isCurrent: () => boolean) => Promise<void>
  enabled?: () => boolean
  valid?: () => boolean
  scope?: () => unknown
  errorMessage: string
  delay?: number
}): AutosaveState {
  const auth = useAuthStore()
  const notifications = useNotificationStore()
  const registry = inject(autosaveRegistry, null)
  const saving = ref(false)
  const error = ref<string | null>(null)
  const authKey = () => JSON.stringify([auth.user?.id, auth.activeOrganizationId])
  const owner = authKey()
  const enabled = () => owner === authKey() && (options.enabled?.() ?? true)
  const valid = computed(() => options.valid?.() ?? true)
  const dirty = computed(() => enabled() && options.dirty())
  const sourceKey = () => JSON.stringify(options.source())
  const scopeKey = () =>
    JSON.stringify([auth.user?.id, auth.activeOrganizationId, options.scope?.()])
  let timer: ReturnType<typeof setTimeout> | undefined
  let running: Promise<boolean> | null = null
  let generation = 0

  function cancelTimer(): void {
    clearTimeout(timer)
    timer = undefined
  }

  function schedule(): void {
    cancelTimer()
    if (!dirty.value || !valid.value) return
    timer = setTimeout(() => void flush(), options.delay ?? 500)
  }

  async function flush(): Promise<boolean> {
    cancelTimer()
    const context = scopeKey()
    const revision = generation
    if (running) {
      if (!(await running)) return false
      if (context !== scopeKey() || revision !== generation) return true
      return flush()
    }
    if (!dirty.value) return true
    if (!valid.value) return false

    const sent = sourceKey()
    const isCurrent = () => context === scopeKey() && revision === generation
    saving.value = true
    error.value = null
    // Defer execution until `running` is assigned, including synchronous failures.
    running = Promise.resolve().then(async () => {
      try {
        if (!isCurrent()) return true
        await options.save(isCurrent)
        return true
      } catch (e: unknown) {
        if (isCurrent()) {
          error.value = e instanceof Error ? e.message : options.errorMessage
          notifications.error(options.errorMessage, error.value)
        }
        return false
      } finally {
        saving.value = false
        running = null
      }
    })
    const success = await running
    if (isCurrent() && dirty.value && sourceKey() !== sent) return flush()
    return success && (!isCurrent() || !dirty.value)
  }

  watch(
    scopeKey,
    () => {
      generation += 1
      cancelTimer()
      error.value = null
    },
    { flush: 'sync' },
  )
  watch([sourceKey, enabled, () => valid.value], () => {
    error.value = null
    schedule()
  })

  const state: AutosaveState = reactive({ saving, error, dirty, valid, flush })
  registry?.add(state)
  onBeforeUnmount(() => {
    registry?.delete(state)
    // No automatic retry after a known failure; the visible Retry action handles it.
    if (!error.value) void flush()
    else cancelTimer()
  })
  return state
}
