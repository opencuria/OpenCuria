import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'

import SendLaterDialog from './SendLaterDialog.vue'

enableAutoUnmount(afterEach)

/** Monday, 16 Nov 2026, 14:20 local time. */
const NOW = new Date(2026, 10, 16, 14, 20)

function field(testId: string): HTMLInputElement {
  return document.querySelector<HTMLInputElement>(`[data-testid="${testId}"]`)!
}

async function setField(testId: string, value: string): Promise<void> {
  const input = field(testId)
  input.value = value
  input.dispatchEvent(new Event('input'))
  await flushPromises()
}

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(SendLaterDialog, {
    props: { open: true, ...props },
    attachTo: document.body,
  })
}

describe('SendLaterDialog', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('starts at the next full hour with today as the earliest date', async () => {
    mountDialog()
    await flushPromises()

    const dialog = document.querySelector('[data-testid="send-later-dialog"]')!
    expect(dialog.textContent).toContain('Send later')
    expect(field('send-later-date').value).toBe('2026-11-16')
    expect(field('send-later-date').min).toBe('2026-11-16')
    expect(field('send-later-time').value).toBe('15:00')
    expect(document.querySelector('[data-testid="send-later-error"]')).toBeNull()
    expect(document.querySelector('[data-testid="send-later-confirm"]')?.textContent).toContain(
      'Schedule',
    )
  })

  it('starts at the current time of a message being rescheduled, unless it already passed', async () => {
    const wrapper = mountDialog({
      initial: new Date(2026, 10, 18, 9, 30).toISOString(),
      actionLabel: 'Reschedule',
    })
    await flushPromises()
    expect(field('send-later-date').value).toBe('2026-11-18')
    expect(field('send-later-time').value).toBe('09:30')
    expect(document.querySelector('[data-testid="send-later-confirm"]')?.textContent).toContain(
      'Reschedule',
    )

    await wrapper.setProps({ open: false })
    await wrapper.setProps({ open: true, initial: new Date(2026, 10, 16, 9, 0) })
    await flushPromises()
    expect(field('send-later-date').value).toBe('2026-11-16')
    expect(field('send-later-time').value).toBe('15:00')
  })

  it('confirms the chosen local time and closes', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    await setField('send-later-date', '2026-11-20')
    await setField('send-later-time', '08:45')

    document.querySelector<HTMLButtonElement>('[data-testid="send-later-confirm"]')!.click()
    await flushPromises()

    expect(wrapper.emitted('confirm')).toEqual([[new Date(2026, 10, 20, 8, 45)]])
    expect(wrapper.emitted('update:open')).toEqual([[false]])
  })

  it('explains an invalid time inline and does not confirm it', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    await setField('send-later-time', '13:00')

    const error = document.querySelector('[data-testid="send-later-error"]')
    expect(error?.textContent).toContain('Choose a time in the future.')
    expect(error?.getAttribute('role')).toBe('alert')
    expect(field('send-later-time').getAttribute('aria-invalid')).toBe('true')
    const confirm = document.querySelector<HTMLButtonElement>('[data-testid="send-later-confirm"]')!
    expect(confirm.disabled).toBe(true)
    document
      .querySelector<HTMLFormElement>('form')!
      .dispatchEvent(new Event('submit', { cancelable: true }))
    await flushPromises()
    expect(wrapper.emitted('confirm')).toBeUndefined()

    await setField('send-later-date', '2028-01-01')
    expect(document.querySelector('[data-testid="send-later-error"]')?.textContent).toContain(
      'Choose a time within a year.',
    )
  })

  it('re-checks the time against the clock when confirming', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    await setField('send-later-time', '14:30')
    vi.setSystemTime(new Date(2026, 10, 16, 14, 31))

    document.querySelector<HTMLButtonElement>('[data-testid="send-later-confirm"]')!.click()
    await flushPromises()

    expect(wrapper.emitted('confirm')).toBeUndefined()
    expect(document.querySelector('[data-testid="send-later-error"]')?.textContent).toContain(
      'Choose a time in the future.',
    )
  })

  it('closes without a time on cancel', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    document.querySelector<HTMLButtonElement>('[data-testid="send-later-cancel"]')!.click()
    await flushPromises()

    expect(wrapper.emitted('update:open')).toEqual([[false]])
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })
})
