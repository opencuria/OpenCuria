import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import { useNotificationStore } from '@/stores/notifications'
import { copyToClipboard } from './clipboard'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

describe('copyToClipboard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('copies and toasts success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const notifications = useNotificationStore()
    const info = vi.spyOn(notifications, 'info')

    expect(await copyToClipboard('abc123', 'commit hash')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('abc123')
    expect(info).toHaveBeenCalled()
  })

  it('never rejects and toasts an error on failure', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.assign(navigator, { clipboard: { writeText } })
    const notifications = useNotificationStore()
    const error = vi.spyOn(notifications, 'error')

    await expect(copyToClipboard('abc123', 'commit hash')).resolves.toBe(false)
    expect(error).toHaveBeenCalled()
    await flushPromises()
    await nextTick()
  })
})
