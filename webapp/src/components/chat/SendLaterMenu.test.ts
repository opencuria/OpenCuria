import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'

import SendLaterMenu from './SendLaterMenu.vue'
import SendLaterDialog from './SendLaterDialog.vue'
import { sendLaterOptions } from '@opencuria/chat-core/sendLater'

enableAutoUnmount(afterEach)

/** Wednesday, 18 Nov 2026, 14:20 local time. */
const NOW = new Date(2026, 10, 18, 14, 20)

function mountMenu(props: Record<string, unknown> = {}) {
  return mount(SendLaterMenu, {
    props,
    slots: { default: '<button type="button" data-testid="trigger">Later</button>' },
    attachTo: document.body,
  })
}

function menuItem(testId: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-testid="${testId}"]`)
}

describe('SendLaterMenu', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('opens from its trigger with the presets of the moment it opens', async () => {
    const wrapper = mountMenu()
    // Presets count from opening the menu, not from mounting it.
    vi.setSystemTime(new Date(2026, 10, 18, 16, 5))
    await wrapper.get('[data-testid="trigger"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    const menu = menuItem('send-later-menu')!
    expect(menu.textContent).toContain('Send later')
    const [inOneHour, tomorrow, monday] = sendLaterOptions(new Date(2026, 10, 18, 16, 5))
    expect(menuItem('send-later-in-1-hour')?.textContent).toContain(inOneHour!.detail)
    expect(menuItem('send-later-in-1-hour')?.textContent).toContain('In 1 hour')
    expect(menuItem('send-later-tomorrow')?.textContent).toContain(tomorrow!.detail)
    expect(menuItem('send-later-monday')?.textContent).toContain(monday!.label)
    expect(menuItem('send-later-custom')?.textContent).toContain('Choose date & time…')

    menuItem('send-later-tomorrow')!.click()
    await flushPromises()
    expect(wrapper.emitted('pick')).toEqual([[new Date(2026, 10, 19, 9, 0)]])
  })

  it('stays closed while disabled', async () => {
    const wrapper = mountMenu({ disabled: true })
    await wrapper.get('[data-testid="trigger"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(menuItem('send-later-menu')).toBeNull()
  })

  it('asks for a custom date and time and emits it', async () => {
    const wrapper = mountMenu({ initial: new Date(2026, 10, 20, 10, 0), actionLabel: 'Reschedule' })
    await wrapper.get('[data-testid="trigger"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    menuItem('send-later-custom')!.click()
    await flushPromises()
    const dialog = wrapper.getComponent(SendLaterDialog)
    expect(dialog.props()).toMatchObject({
      open: true,
      initial: new Date(2026, 10, 20, 10, 0),
      actionLabel: 'Reschedule',
    })

    dialog.vm.$emit('confirm', new Date(2026, 10, 20, 11, 0))
    expect(wrapper.emitted('pick')).toEqual([[new Date(2026, 10, 20, 11, 0)]])
  })
})
