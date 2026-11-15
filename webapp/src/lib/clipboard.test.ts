import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useNotificationStore } from '@/stores/notifications'
import { useClipboardStore } from '@/stores/clipboard'
import { copyToClipboard, writeClipboardText } from './clipboard'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const originalClipboard = navigator.clipboard
const originalClipboardItem = globalThis.ClipboardItem
const originalExecCommand = document.execCommand

type ClipboardItemValues = Record<string, unknown>

class TestClipboardItem {
  constructor(readonly values: ClipboardItemValues) {}
}

function stubClipboard(value: unknown): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value,
  })
}

function stubClipboardItem(): void {
  vi.stubGlobal('ClipboardItem', TestClipboardItem)
}

function requestLoadFailed(store: ReturnType<typeof useClipboardStore>): boolean | undefined {
  return (store.request as { loadFailed?: boolean } | null)?.loadFailed
}

function stubExecCommand(
  result: boolean,
  dispatchCopy = true,
): {
  exec: ReturnType<typeof vi.fn>
  setData: ReturnType<typeof vi.fn>
} {
  const setData = vi.fn()
  const exec = vi.fn((command: string) => {
    if (command === 'copy' && dispatchCopy) {
      const event = new Event('copy', { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'clipboardData', {
        value: { setData },
      })
      document.dispatchEvent(event)
    }
    return result
  })
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    writable: true,
    value: exec,
  })
  return { exec, setData }
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

describe('clipboard operations', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    document.body.innerHTML = ''
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: originalExecCommand,
    })
    stubClipboard(originalClipboard)
    if (originalClipboardItem) vi.stubGlobal('ClipboardItem', originalClipboardItem)
    document.body.innerHTML = ''
  })

  it('uses the modern text writer and returns its actual success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    const { exec } = stubExecCommand(false)

    await expect(writeClipboardText('abc123')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('abc123')
    expect(exec).not.toHaveBeenCalled()
  })

  it('never throws when both low-level write paths fail', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('permission denied'))
    stubClipboard({ writeText })
    const { exec, setData } = stubExecCommand(false)

    await expect(writeClipboardText('secret-value')).resolves.toBe(false)
    expect(exec).toHaveBeenCalledWith('copy')
    expect(setData).toHaveBeenCalledWith('text/plain', 'secret-value')
  })

  it('uses an HTTP legacy copy event with plain text and preserves focus and selection', async () => {
    stubClipboard(undefined)
    const { exec, setData } = stubExecCommand(true)
    const target = document.createElement('p')
    target.textContent = 'selected text'
    document.body.appendChild(target)
    const focusTarget = document.createElement('button')
    document.body.appendChild(focusTarget)
    focusTarget.focus()
    const selection = document.getSelection()!
    const range = document.createRange()
    range.selectNodeContents(target)
    selection.removeAllRanges()
    selection.addRange(range)
    const selectedRange = selection.getRangeAt(0).cloneRange()

    await expect(writeClipboardText('copy payload')).resolves.toBe(true)

    expect(exec).toHaveBeenCalledWith('copy')
    expect(setData).toHaveBeenCalledOnce()
    expect(setData).toHaveBeenCalledWith('text/plain', 'copy payload')
    expect(document.activeElement).toBe(focusTarget)
    expect(selection.rangeCount).toBe(1)
    expect(selection.getRangeAt(0).toString()).toBe('selected text')
    expect(selection.getRangeAt(0).commonAncestorContainer).toBe(
      selectedRange.commonAncestorContainer,
    )
    expect(document.querySelector('textarea')).toBeNull()
    expect(document.querySelector('span')).toBeNull()
  })

  it('requires both execCommand success and an explicit copy-event payload', async () => {
    stubClipboard(undefined)
    const commandFailed = stubExecCommand(false)
    await expect(writeClipboardText('text')).resolves.toBe(false)
    expect(commandFailed.setData).toHaveBeenCalledWith('text/plain', 'text')

    const noCopyEvent = stubExecCommand(true, false)
    await expect(writeClipboardText('text')).resolves.toBe(false)
    expect(noCopyEvent.setData).not.toHaveBeenCalled()
  })

  it('toasts only the label on confirmed OS copy success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')

    await expect(copyToClipboard('private payload', 'commit hash')).resolves.toBe(true)

    expect(info).toHaveBeenCalledOnce()
    expect(info).toHaveBeenCalledWith('Copied commit hash')
    expect(JSON.stringify(info.mock.calls)).not.toContain('private payload')
  })

  it('uses the supplied iframe realm for its Blob, ClipboardItem, and navigator clipboard', async () => {
    const frame = document.createElement('iframe')
    document.body.appendChild(frame)
    const context = frame.contentWindow as Window & typeof globalThis
    class FrameClipboardItem {
      constructor(readonly values: Record<string, unknown>) {}
    }
    const write = vi.fn(async (items: FrameClipboardItem[]) => {
      expect(items[0]).toBeInstanceOf(FrameClipboardItem)
      const blob = (await items[0]!.values['text/plain']) as Blob
      expect(blob).toBeInstanceOf(context.Blob as typeof Blob)
      expect(blob.type).toBe('text/plain')
    })
    Object.defineProperty(context, 'ClipboardItem', {
      configurable: true,
      value: FrameClipboardItem,
    })
    Object.defineProperty(context.navigator, 'clipboard', {
      configurable: true,
      value: { write },
    })

    await expect(
      copyToClipboard(async () => 'frame payload', 'artifact', {
        context,
        success: 'silent',
      }),
    ).resolves.toBe(true)

    expect(write).toHaveBeenCalledOnce()
    expect((write.mock.calls[0]![0][0] as FrameClipboardItem).values['text/plain']).toBeInstanceOf(
      Promise,
    )
    frame.remove()
  })

  it('starts ClipboardItem writing before an async producer settles and invokes it once', async () => {
    stubClipboardItem()
    const text = deferred<string>()
    const write = vi.fn(async (items: TestClipboardItem[]) => {
      const blob = items[0]!.values['text/plain'] as Promise<Blob>
      expect(blob).toBeInstanceOf(Promise)
      await blob
    })
    stubClipboard({ write })
    const loader = vi.fn(() => text.promise)
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')

    const operation = copyToClipboard(loader, 'artifact', { success: 'silent' })

    expect(loader).toHaveBeenCalledOnce()
    expect(write).toHaveBeenCalledOnce()
    expect(write.mock.calls[0]![0][0]).toBeInstanceOf(TestClipboardItem)
    expect(useClipboardStore().request).toBeNull()
    text.resolve('async payload')

    await expect(operation).resolves.toBe(true)
    expect(loader).toHaveBeenCalledOnce()
    expect(info).not.toHaveBeenCalled()
  })

  it('uses the no-dialog legacy async fallback with the exact produced text', async () => {
    stubClipboard(undefined)
    vi.stubGlobal('ClipboardItem', undefined)
    const { exec, setData } = stubExecCommand(true)
    const secret = 'exact async clipboard payload\nsecond line'
    const loader = vi.fn(async () => secret)
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')
    const error = vi.spyOn(notifications, 'error')

    await expect(
      copyToClipboard(loader, 'VM clipboard', { fallback: 'none', success: 'silent' }),
    ).resolves.toBe(true)

    expect(loader).toHaveBeenCalledOnce()
    expect(exec).toHaveBeenCalledWith('copy')
    expect(setData).toHaveBeenCalledOnce()
    expect(setData).toHaveBeenCalledWith('text/plain', secret)
    expect(info).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect(JSON.stringify(info.mock.calls.concat(error.mock.calls))).not.toContain(secret)
  })

  it('reports blocked async legacy copy generically and preserves focused selection', async () => {
    stubClipboard(undefined)
    vi.stubGlobal('ClipboardItem', undefined)
    const { exec, setData } = stubExecCommand(true, false)
    const secret = 'private async text'
    const selected = document.createElement('p')
    selected.textContent = 'keep this selection'
    const focusTarget = document.createElement('button')
    document.body.append(selected, focusTarget)
    focusTarget.focus()
    const selection = document.getSelection()!
    const range = document.createRange()
    range.selectNodeContents(selected)
    selection.removeAllRanges()
    selection.addRange(range)
    const notifications = useNotificationStore()
    const error = vi.spyOn(notifications, 'error')

    await expect(
      copyToClipboard(async () => secret, 'VM clipboard', { fallback: 'none' }),
    ).resolves.toBe(false)

    expect(exec).toHaveBeenCalledWith('copy')
    expect(setData).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(focusTarget)
    expect(selection.toString()).toBe('keep this selection')
    expect(document.querySelector('span')).toBeNull()
    expect(error).toHaveBeenCalledOnce()
    expect(error).toHaveBeenCalledWith('Failed to copy VM clipboard', expect.any(String))
    expect(JSON.stringify(error.mock.calls)).not.toContain(secret)
  })

  it('does not run the legacy async fallback after its producer is aborted', async () => {
    stubClipboard(undefined)
    vi.stubGlobal('ClipboardItem', undefined)
    const { exec } = stubExecCommand(true)
    const text = deferred<string>()
    const controller = new AbortController()
    const notifications = useNotificationStore()
    const error = vi.spyOn(notifications, 'error')
    const operation = copyToClipboard(() => text.promise, 'VM clipboard', {
      fallback: 'none',
      signal: controller.signal,
    })

    controller.abort()
    text.resolve('late private text')
    await expect(operation).resolves.toBe(false)

    expect(exec).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect(useClipboardStore().request).toBeNull()
  })

  it('offers Copy now after async Clipboard API rejection without a late execCommand', async () => {
    stubClipboardItem()
    const writeText = vi.fn().mockResolvedValue(undefined)
    const write = vi.fn().mockRejectedValue(new Error('gesture expired'))
    stubClipboard({ write, writeText })
    const { exec } = stubExecCommand(false, false)
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')
    const operation = copyToClipboard(async () => 'retry payload', 'artifact')
    const store = useClipboardStore()

    await vi.waitFor(() => expect(store.request?.text).toBe('retry payload'))
    expect(write).toHaveBeenCalledOnce()
    expect(exec).not.toHaveBeenCalled()
    await store.copyNow()

    await expect(operation).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('retry payload')
    expect(exec).not.toHaveBeenCalled()
    expect(store.request).toBeNull()
    expect(JSON.stringify(info.mock.calls)).not.toContain('retry payload')
  })

  it('opens a loading manual-copy request immediately when async clipboard writing is unavailable', async () => {
    stubClipboard({ writeText: vi.fn() })
    const text = deferred<string>()
    const loader = vi.fn(() => text.promise)
    const store = useClipboardStore()
    const operation = copyToClipboard(loader, 'artifact', { success: 'silent' })

    expect(loader).toHaveBeenCalledOnce()
    expect(store.request).toMatchObject({
      label: 'artifact',
      text: '',
      loading: true,
      error: '',
    })
    text.resolve('loaded payload')
    await vi.waitFor(() => expect(store.request?.text).toBe('loaded payload'))

    store.finish()
    await expect(operation).resolves.toBe(false)
  })

  it('shows a generic error when the producer throws, without retaining or exposing payload', async () => {
    const secret = 'never expose this content'
    const notifications = useNotificationStore()
    const error = vi.spyOn(notifications, 'error')
    const loader = vi.fn(() => {
      throw new Error(secret)
    })

    await expect(copyToClipboard(loader, 'artifact')).resolves.toBe(false)

    expect(useClipboardStore().request).toBeNull()
    expect(error).toHaveBeenCalledOnce()
    expect(JSON.stringify(error.mock.calls)).not.toContain(secret)
  })

  it('keeps a failed async load in the manual dialog with generic error text', async () => {
    stubClipboard({})
    const text = deferred<string>()
    const operation = copyToClipboard(() => text.promise, 'artifact')
    const store = useClipboardStore()
    expect(store.request?.loading).toBe(true)
    expect(store.request?.error).toBe('')

    text.reject(new Error('private fetch detail'))
    await vi.waitFor(() => expect(store.request?.error).toBeTruthy())
    expect(requestLoadFailed(store)).toBe(true)

    expect(store.request).toMatchObject({
      label: 'artifact',
      text: '',
      loading: false,
    })
    expect(store.request?.error).toBeTruthy()
    await store.copyNow()
    expect(store.request?.error).toBeTruthy()
    store.finish()
    await expect(operation).resolves.toBe(false)
  })

  it('aborts pending fallback work without stale content or toast after late completion', async () => {
    stubClipboard({})
    const text = deferred<string>()
    const controller = new AbortController()
    const store = useClipboardStore()
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')
    const error = vi.spyOn(notifications, 'error')
    const operation = copyToClipboard(() => text.promise, 'artifact', {
      signal: controller.signal,
    })

    expect(store.request?.loading).toBe(true)
    controller.abort()
    await expect(operation).resolves.toBe(false)
    expect(store.request).toBeNull()

    text.resolve('late private content')
    await Promise.resolve()
    expect(store.request).toBeNull()
    expect(info).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()

    const lateFailure = deferred<string>()
    const failedController = new AbortController()
    const failedOperation = copyToClipboard(() => lateFailure.promise, 'artifact', {
      signal: failedController.signal,
    })
    failedController.abort()
    await expect(failedOperation).resolves.toBe(false)
    lateFailure.reject(new Error('private late failure'))
    await Promise.resolve()

    expect(store.request).toBeNull()
    expect(info).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })
})

describe('clipboard cancellation boundaries', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    stubClipboard(originalClipboard)
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: originalExecCommand,
    })
  })

  it('rejects a deferred Blob after cancellation before payload delivery', async () => {
    stubClipboardItem()
    const text = deferred<string>()
    const controller = new AbortController()
    let blob!: Promise<Blob>
    const write = vi.fn(async (items: TestClipboardItem[]) => {
      blob = items[0]!.values['text/plain'] as Promise<Blob>
      await blob
    })
    stubClipboard({ write })
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')
    const operation = copyToClipboard(() => text.promise, 'VM clipboard', {
      signal: controller.signal,
    })
    expect(write).toHaveBeenCalledOnce()
    controller.abort()
    text.resolve('stale VM text')
    await expect(blob).rejects.toThrow('Copy cancelled')
    await expect(operation).resolves.toBe(false)
    expect(useClipboardStore().request).toBeNull()
    expect(info).not.toHaveBeenCalled()
  })

  it('does not report success for a native write completed after cancellation', async () => {
    const pending = deferred<void>()
    const controller = new AbortController()
    stubClipboard({ writeText: vi.fn(() => pending.promise) })
    const info = vi.spyOn(useNotificationStore(), 'info')
    const operation = copyToClipboard('old text', 'message', { signal: controller.signal })
    controller.abort()
    pending.resolve()
    await expect(operation).resolves.toBe(false)
    expect(info).not.toHaveBeenCalled()
  })

  it('does not attempt legacy fallback after an aborted modern write rejects', async () => {
    const pending = deferred<void>()
    const controller = new AbortController()
    stubClipboard({ writeText: vi.fn(() => pending.promise) })
    const { exec } = stubExecCommand(true)
    const operation = copyToClipboard('old text', 'message', { signal: controller.signal })
    controller.abort()
    pending.reject(new Error('denied'))
    await expect(operation).resolves.toBe(false)
    expect(exec).not.toHaveBeenCalled()
    expect(useClipboardStore().request).toBeNull()
  })
})
