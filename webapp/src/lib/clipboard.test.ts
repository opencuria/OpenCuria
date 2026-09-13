import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useNotificationStore } from '@/stores/notifications'
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

function stubClipboard(value: unknown): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value,
  })
}

function stubExecCommand(result: boolean): ReturnType<typeof vi.fn> {
  const exec = vi.fn().mockReturnValue(result)
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    writable: true,
    value: exec,
  })
  return exec
}

describe('writeClipboardText / copyToClipboard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    stubClipboard(originalClipboard)
  })

  it('copies and toasts success via the Clipboard API', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    const exec = stubExecCommand(false)
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')

    expect(await copyToClipboard('abc123', 'commit hash')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('abc123')
    expect(exec).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledWith('Copied commit hash', 'abc123')
  })

  it('falls back to execCommand when clipboard is missing', async () => {
    stubClipboard(undefined)
    const exec = stubExecCommand(true)
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')
    const error = vi.spyOn(notifications, 'error')

    expect(await copyToClipboard('main', 'branch name')).toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
    expect(info).toHaveBeenCalledWith('Copied branch name', 'main')
    expect(error).not.toHaveBeenCalled()
  })

  it('falls back to execCommand when writeText rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    stubClipboard({ writeText })
    const exec = stubExecCommand(true)

    expect(await writeClipboardText('abc123')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('abc123')
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('never rejects and toasts an error when both write paths fail', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    stubClipboard({ writeText })
    stubExecCommand(false)
    const notifications = useNotificationStore()
    const error = vi.spyOn(notifications, 'error')

    await expect(copyToClipboard('abc123', 'commit hash')).resolves.toBe(false)
    expect(error).toHaveBeenCalledWith('Failed to copy commit hash', 'Clipboard unavailable')
  })
})
