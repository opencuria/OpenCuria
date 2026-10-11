import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import CopyButton from './CopyButton.vue'

const clipboardMocks = vi.hoisted(() => ({
  copyToClipboard: vi.fn(),
}))
vi.mock('@/lib/clipboard', () => ({
  copyToClipboard: clipboardMocks.copyToClipboard,
}))

function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
} {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

describe('CopyButton', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
    clipboardMocks.copyToClipboard.mockResolvedValue(true)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('passes source, label, silent feedback, and an abort signal', async () => {
    const wrapper = mount(CopyButton, {
      props: { text: 'payload', label: 'artifact' },
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(clipboardMocks.copyToClipboard).toHaveBeenCalledOnce()
    expect(clipboardMocks.copyToClipboard).toHaveBeenCalledWith(
      'payload',
      'artifact',
      expect.objectContaining({ success: 'silent', signal: expect.any(AbortSignal) }),
    )
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copied')
    expect(wrapper.get('button').attributes('title')).toBe('Copied')
    expect(wrapper.get('button').attributes('data-copied')).toBe('true')
  })

  it('ignores reentrant clicks while a copy is pending', async () => {
    const pending = deferred<boolean>()
    clipboardMocks.copyToClipboard.mockReturnValue(pending.promise)
    const wrapper = mount(CopyButton, {
      props: { text: 'payload', label: 'artifact' },
    })

    await wrapper.get('button').trigger('click')
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.get('button').trigger('click')

    expect(clipboardMocks.copyToClipboard).toHaveBeenCalledOnce()
    pending.resolve(true)
    await flushPromises()
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copied')
  })

  it('shows copied feedback only for actual success and resets it after two seconds', async () => {
    vi.useFakeTimers()
    clipboardMocks.copyToClipboard.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const wrapper = mount(CopyButton, {
      props: { text: 'payload', label: 'artifact' },
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copy artifact')
    expect(wrapper.get('button').attributes('data-copied')).toBe('false')

    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copied')
    await vi.advanceTimersByTimeAsync(1999)
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copied')
    await vi.advanceTimersByTimeAsync(1)
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copy artifact')
  })

  it('aborts a fallback and resets when its source prop changes', async () => {
    const pending = deferred<boolean>()
    clipboardMocks.copyToClipboard.mockReturnValue(pending.promise)
    const wrapper = mount(CopyButton, {
      props: { text: 'first payload', label: 'artifact' },
    })

    await wrapper.get('button').trigger('click')
    const options = clipboardMocks.copyToClipboard.mock.calls[0]![2] as {
      signal: AbortSignal
    }
    expect(options.signal.aborted).toBe(false)

    await wrapper.setProps({ text: 'second payload' })
    expect(options.signal.aborted).toBe(true)
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copy artifact')
    pending.resolve(true)
    await flushPromises()
    expect(wrapper.get('button').attributes('aria-label')).toBe('Copy artifact')
  })

  it('aborts pending fallback work on unmount', async () => {
    const pending = deferred<boolean>()
    clipboardMocks.copyToClipboard.mockReturnValue(pending.promise)
    const wrapper = mount(CopyButton, {
      props: { text: 'payload', label: 'artifact' },
    })

    await wrapper.get('button').trigger('click')
    const options = clipboardMocks.copyToClipboard.mock.calls[0]![2] as {
      signal: AbortSignal
    }
    wrapper.unmount()

    expect(options.signal.aborted).toBe(true)
    pending.resolve(true)
    await flushPromises()
  })

  it('does not invoke copying when disabled', async () => {
    const wrapper = mount(CopyButton, {
      props: { text: 'payload', label: 'artifact', disabled: true },
    })

    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.get('button').trigger('click')
    expect(clipboardMocks.copyToClipboard).not.toHaveBeenCalled()
  })
})
