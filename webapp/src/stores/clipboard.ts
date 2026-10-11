/** Ephemeral manual-copy fallback; never persisted, logged, or sent to the server. */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { writeClipboardText } from '@/lib/clipboardWrite'

export const useClipboardStore = defineStore('clipboard', () => {
  const request = ref<{
    label: string
    text: string
    loading: boolean
    loadFailed: boolean
    error: string
  } | null>(null)
  const busy = ref(false)
  let resolveRequest: ((ok: boolean) => void) | null = null
  let requestSignal: AbortSignal | undefined
  let removeAbortListener: (() => void) | null = null

  function complete(ok: boolean): void {
    const resolve = resolveRequest
    resolveRequest = null
    removeAbortListener?.()
    removeAbortListener = null
    requestSignal = undefined
    request.value = null
    busy.value = false
    resolve?.(ok)
  }

  function finish(): void {
    complete(false)
  }

  /** A new request replaces any older one and releases its retained content. */
  function requestCopy(
    text: string | Promise<string>,
    label: string,
    signal?: AbortSignal,
  ): Promise<boolean> {
    finish()
    if (signal?.aborted) return Promise.resolve(false)
    const current = { label, text: '', loading: true, loadFailed: false, error: '' }
    requestSignal = signal
    request.value = current
    const result = new Promise<boolean>((resolve) => {
      resolveRequest = resolve
    })
    if (signal) {
      const onAbort = (): void => finish()
      signal.addEventListener('abort', onAbort, { once: true })
      removeAbortListener = () => signal.removeEventListener('abort', onAbort)
    }
    const resolver = resolveRequest
    void Promise.resolve(text).then(
      (value) => {
        if (resolveRequest !== resolver || !request.value) return
        request.value.text = value
        request.value.loading = false
      },
      () => {
        if (resolveRequest !== resolver || !request.value) return
        request.value.loading = false
        request.value.loadFailed = true
        request.value.error = 'Clipboard content could not be loaded. Close and try again.'
      },
    )
    return result
  }

  /** Must be called directly from the retry button's trusted click. */
  async function copyNow(): Promise<void> {
    const current = request.value
    if (!current || current.loading || current.loadFailed || busy.value) return
    const resolver = resolveRequest
    busy.value = true
    const ok = await writeClipboardText(current.text, requestSignal)
    if (resolveRequest !== resolver) return
    busy.value = false
    if (ok) complete(true)
    else current.error = 'Browser copy is blocked. Select the text and press ⌘C or Ctrl+C.'
  }

  return { request, busy, requestCopy, copyNow, finish }
})
