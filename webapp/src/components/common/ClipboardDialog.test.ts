import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import { useClipboardStore } from '@/stores/clipboard'
import ClipboardDialog from './ClipboardDialog.vue'

const originalClipboard = navigator.clipboard
const wrappers: VueWrapper[] = []

function stubClipboard(value: unknown): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value,
  })
}

function mountDialog(): VueWrapper {
  const wrapper = mount(ClipboardDialog, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

function dialogButton(label: string): HTMLButtonElement {
  const found = Array.from(document.body.querySelectorAll('button')).find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!found) throw new Error(`Missing ${label} button`)
  return found
}

describe('ClipboardDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    document.body.innerHTML = ''
  })

  afterEach(() => {
    wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.restoreAllMocks()
    stubClipboard(originalClipboard)
    document.body.innerHTML = ''
  })

  it('renders the shared dialog with selectable read-only text and closes via finish()', async () => {
    const store = useClipboardStore()
    const result = store.requestCopy(Promise.resolve('private snippet'), 'code')
    const finish = vi.spyOn(store, 'finish')
    mountDialog()
    await flushPromises()
    await nextTick()

    const title = document.body.querySelector('[data-slot="dialog-title"]')
    const textarea = document.body.querySelector(
      'textarea[aria-label="Text to copy"]',
    ) as HTMLTextAreaElement | null
    expect(title?.textContent).toBe('Copy code')
    expect(textarea).not.toBeNull()
    expect(textarea).toMatchObject({ value: 'private snippet', readOnly: true })

    textarea!.focus()
    expect((textarea as HTMLTextAreaElement).selectionStart).toBe(0)
    expect((textarea as HTMLTextAreaElement).selectionEnd).toBe('private snippet'.length)

    dialogButton('Close').click()
    await flushPromises()

    expect(finish).toHaveBeenCalled()
    expect(finish.mock.calls.every((args) => args.length === 0)).toBe(true)
    expect(store.request).toBeNull()
    await expect(result).resolves.toBe(false)
  })

  it('disables Copy now while content loads, then enables it once available', async () => {
    let resolve!: (text: string) => void
    const content = new Promise<string>((resolvePromise) => {
      resolve = resolvePromise
    })
    const store = useClipboardStore()
    store.requestCopy(content, 'artifact')
    mountDialog()
    await nextTick()

    expect(document.body.textContent).toContain('Loading clipboard content…')
    expect(dialogButton('Copy now').disabled).toBe(true)
    expect(document.body.querySelector('textarea')).toBeNull()

    resolve('ready text')
    await flushPromises()
    await nextTick()
    expect(dialogButton('Copy now').disabled).toBe(false)
    expect((document.body.querySelector('textarea') as HTMLTextAreaElement).value).toBe(
      'ready text',
    )
  })

  it('exposes the current generic error state for a failed content load', async () => {
    let reject!: (reason?: unknown) => void
    const content = new Promise<string>((_resolve, rejectPromise) => {
      reject = rejectPromise
    })
    const store = useClipboardStore()
    store.requestCopy(content, 'artifact')
    mountDialog()
    reject(new Error('sensitive HTTP response'))
    await flushPromises()
    await nextTick()

    expect(dialogButton('Copy now').disabled).toBe(true)
    expect(document.body.querySelector('textarea')).not.toBeNull()
    const alert = document.body.querySelector('[role="alert"]')
    expect(alert?.textContent).toBe('Clipboard content could not be loaded. Close and try again.')
    expect(document.body.textContent).not.toContain('sensitive HTTP response')
  })

  it('keeps the manual fallback open when Copy now is blocked', async () => {
    stubClipboard(undefined)
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn().mockReturnValue(false),
    })
    const store = useClipboardStore()
    const result = store.requestCopy(Promise.resolve('private copy text'), 'artifact')
    mountDialog()
    await flushPromises()

    dialogButton('Copy now').click()
    await flushPromises()

    expect(store.request).toMatchObject({ text: 'private copy text', loading: false })
    expect(document.body.textContent).toContain(
      'Browser copy is blocked. Select the text and press ⌘C or Ctrl+C.',
    )
    expect(document.body.querySelector('[data-slot="dialog-content"]')).not.toBeNull()
    store.finish()
    await expect(result).resolves.toBe(false)
  })

  it('finishes the pending request when the dialog is dismissed', async () => {
    const store = useClipboardStore()
    const result = store.requestCopy(Promise.resolve('private copy text'), 'artifact')
    const finish = vi.spyOn(store, 'finish')
    mountDialog()
    await flushPromises()

    const close = document.body.querySelector(
      '[data-slot="dialog-close"]',
    ) as HTMLButtonElement | null
    expect(close).not.toBeNull()
    close!.click()
    await flushPromises()

    expect(finish).toHaveBeenCalled()
    expect(finish.mock.calls.every((args) => args.length === 0)).toBe(true)
    expect(store.request).toBeNull()
    await expect(result).resolves.toBe(false)
  })
})
