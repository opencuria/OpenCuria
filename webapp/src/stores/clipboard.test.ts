import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useClipboardStore } from './clipboard'

const originalClipboard = navigator.clipboard
const originalExecCommand = document.execCommand

function requestLoadFailed(store: ReturnType<typeof useClipboardStore>): boolean | undefined {
  return (store.request as { loadFailed?: boolean } | null)?.loadFailed
}

function stubClipboard(value: unknown): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value,
  })
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
} {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('clipboard store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    stubClipboard(originalClipboard)
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: originalExecCommand,
    })
  })

  it('publishes an async request as loading, then retains its loaded text', async () => {
    const content = deferred<string>()
    const store = useClipboardStore()
    const result = store.requestCopy(content.promise, 'artifact')

    expect(store.request).toMatchObject({
      label: 'artifact',
      text: '',
      loading: true,
      error: '',
    })
    expect(requestLoadFailed(store)).toBe(false)

    content.resolve('loaded text')
    await vi.waitFor(() =>
      expect(store.request).toMatchObject({
        text: 'loaded text',
        loading: false,
      }),
    )

    store.finish()
    await expect(result).resolves.toBe(false)
  })

  it('marks a rejected loader as unavailable without retaining an error payload', async () => {
    const content = deferred<string>()
    const store = useClipboardStore()
    const result = store.requestCopy(content.promise, 'artifact')
    content.reject(new Error('sensitive response detail'))

    await vi.waitFor(() => expect(store.request?.error).toBeTruthy())
    expect(requestLoadFailed(store)).toBe(true)
    expect(store.request).toMatchObject({
      label: 'artifact',
      text: '',
      loading: false,
    })
    expect(store.request?.error).not.toContain('sensitive response detail')
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    await store.copyNow()
    expect(writeText).not.toHaveBeenCalled()
    expect(store.request?.loadFailed).toBe(true)
    store.finish()
    await expect(result).resolves.toBe(false)
  })

  it('replaces an older request and ignores its late completion', async () => {
    const first = deferred<string>()
    const store = useClipboardStore()
    const firstResult = store.requestCopy(first.promise, 'artifact')
    const secondResult = store.requestCopy(Promise.resolve('second text'), 'artifact')

    await vi.waitFor(() =>
      expect(store.request).toMatchObject({
        label: 'artifact',
        text: 'second text',
        loading: false,
      }),
    )
    first.resolve('stale first text')
    await Promise.resolve()

    expect(store.request).toMatchObject({ label: 'artifact', text: 'second text' })
    await expect(firstResult).resolves.toBe(false)
    store.finish()
    await expect(secondResult).resolves.toBe(false)
  })

  it('clears the request on abort and ignores late loader completion', async () => {
    const content = deferred<string>()
    const controller = new AbortController()
    const store = useClipboardStore()
    const result = store.requestCopy(content.promise, 'artifact', controller.signal)

    expect(store.request?.loading).toBe(true)
    controller.abort()
    await expect(result).resolves.toBe(false)
    expect(store.request).toBeNull()
    content.resolve('late content')
    await Promise.resolve()
    expect(store.request).toBeNull()
  })

  it('exposes finish without a caller-controlled success value and clears retained text', async () => {
    const store = useClipboardStore()
    const result = store.requestCopy(Promise.resolve('private text'), 'artifact')
    await vi.waitFor(() => expect(store.request?.text).toBe('private text'))

    store.finish()

    expect(store.request).toBeNull()
    expect(store.busy).toBe(false)
    await expect(result).resolves.toBe(false)
  })

  it('clears and resolves a request only after Copy now confirms OS success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    const store = useClipboardStore()
    const result = store.requestCopy(Promise.resolve('copy this'), 'artifact')
    await vi.waitFor(() => expect(store.request?.loading).toBe(false))

    await store.copyNow()

    await expect(result).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('copy this')
    expect(store.request).toBeNull()
    expect(store.busy).toBe(false)
  })

  it('keeps a blocked manual retry open and reports the generic failure', async () => {
    stubClipboard(undefined)
    const execCommand = vi.fn().mockReturnValue(false)
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    })
    const store = useClipboardStore()
    const result = store.requestCopy(Promise.resolve('copy this'), 'artifact')
    await vi.waitFor(() => expect(store.request?.loading).toBe(false))

    await store.copyNow()

    expect(store.request).toMatchObject({
      label: 'artifact',
      text: 'copy this',
      loading: false,
    })
    expect(store.request?.error).toBeTruthy()
    expect(store.busy).toBe(false)
    expect(execCommand).toHaveBeenCalledWith('copy')
    store.finish()
    await expect(result).resolves.toBe(false)
  })

  it('ignores duplicate Copy now attempts while one OS write is pending', async () => {
    const write = deferred<void>()
    const writeText = vi.fn(() => write.promise)
    stubClipboard({ writeText })
    const store = useClipboardStore()
    store.requestCopy(Promise.resolve('copy this'), 'artifact')
    await vi.waitFor(() => expect(store.request?.loading).toBe(false))

    const firstAttempt = store.copyNow()
    const secondAttempt = store.copyNow()
    expect(store.busy).toBe(true)
    expect(writeText).toHaveBeenCalledOnce()

    write.resolve()
    await Promise.all([firstAttempt, secondAttempt])
    expect(store.request).toBeNull()
    expect(store.busy).toBe(false)
  })
})
