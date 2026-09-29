import { inject, onUnmounted, ref, type ComputedRef, type InjectionKey } from 'vue'
import { pinHarnessMessageKey } from '@/lib/harnessMessagePin'
import type { HarnessPart } from '@/types/harness'

export type RequestHarnessPartDetail = (sessionId: string, partId: string) => Promise<boolean>
export const requestHarnessPartDetailKey: InjectionKey<RequestHarnessPartDetail> = Symbol(
  'requestHarnessPartDetail',
)
export type PinHarnessPartDetail = (sessionId: string, partId: string, pinned: boolean) => void
export const pinHarnessPartDetailKey: InjectionKey<PinHarnessPartDetail> =
  Symbol('pinHarnessPartDetail')

/** Per-row lazy loading state, backed by the chat panel's store action. */
export function useHarnessPartDetail(part: ComputedRef<HarnessPart>) {
  const request = inject(requestHarnessPartDetailKey, null)
  const loading = ref(false)
  const error = ref('')
  const pinMessage = inject(pinHarnessMessageKey, null)
  const pinDetail = inject(pinHarnessPartDetailKey, null)
  let pinnedKey: { sessionId: string; partId: string; messageId: string } | null = null
  onUnmounted(() => {
    if (pinnedKey) {
      pinDetail?.(pinnedKey.sessionId, pinnedKey.partId, false)
      pinMessage?.(pinnedKey.messageId, false)
    }
  })

  function setPinned(pinned: boolean): void {
    if (pinned) {
      pinnedKey = {
        sessionId: part.value.session_id,
        partId: part.value.id,
        messageId: part.value.message_id ?? '',
      }
      pinDetail?.(pinnedKey.sessionId, pinnedKey.partId, true)
      pinMessage?.(pinnedKey.messageId, true)
      return
    }
    if (pinnedKey) {
      pinDetail?.(pinnedKey.sessionId, pinnedKey.partId, false)
      pinMessage?.(pinnedKey.messageId, false)
    }
    pinnedKey = null
  }

  function setExpanded(expanded: boolean): void {
    setPinned(expanded)
  }

  async function load(): Promise<void> {
    if (
      part.value.detail_loaded !== false ||
      part.value.state === 'pending' ||
      part.value.state === 'running' ||
      loading.value ||
      !request
    )
      return
    loading.value = true
    setPinned(true)
    error.value = ''
    try {
      const loaded = await request(part.value.session_id, part.value.id)
      if (!loaded && part.value.detail_loaded === false)
        error.value = 'Details are not available yet'
    } catch (reason: unknown) {
      // Resolve failures locally: child lifecycle hooks must never create
      // unhandled Promise rejections.
      error.value = reason instanceof Error ? reason.message : 'Failed to load details'
    } finally {
      loading.value = false
    }
  }

  return { loading, error, load, setExpanded }
}
