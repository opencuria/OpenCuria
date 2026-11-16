import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessQueueSheet from './HarnessQueueSheet.vue'
import source from './HarnessQueueSheet.vue?raw'
import { formatSendTime } from '@opencuria/chat-core/sendLater'
import type { HarnessScheduledMessage, HarnessSteer } from '@opencuria/chat-core/harness'

function makeSteer(id: string, content: string): HarnessSteer {
  return { id, content, created_at: '2026-10-16T10:00:00Z' }
}

/** Monday, 16 Nov 2026, 14:00 local time. */
const NOW = new Date(2026, 10, 16, 14, 0)
const TOMORROW_9 = new Date(2026, 10, 17, 9, 0)

function makeScheduled(
  id: string,
  overrides: Partial<HarnessScheduledMessage> = {},
): HarnessScheduledMessage {
  return {
    id,
    session_id: 'session-1',
    content: `Later ${id}`,
    send_at: TOMORROW_9.toISOString(),
    status: 'pending',
    error: '',
    skill_ids: [],
    created_at: '2026-11-16T10:00:00Z',
    ...overrides,
  }
}

const menuStubs = {
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuLabel: { template: '<div><slot /></div>' },
  DropdownMenuSeparator: { template: '<hr />' },
  DropdownMenuItem: { template: '<button type="button"><slot /></button>' },
}

function mountScheduled(scheduled: HarnessScheduledMessage[], props = {}) {
  return mount(HarnessQueueSheet, {
    props: { steers: [], scheduled, ...props },
    global: { stubs: menuStubs },
  })
}

describe('HarnessQueueSheet', () => {
  it('lists queued messages in order, one truncated line each, with the full text as tooltip', () => {
    const long = 'Please also update the changelog\nand bump the version in package.json'
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'Use pnpm'), makeSteer('b', long)] },
    })

    const rows = wrapper.findAll('[data-testid="composer-queue-row"]')
    expect(rows).toHaveLength(2)
    const texts = wrapper.findAll('[data-testid="composer-queue-text"]')
    expect(texts[0]!.text()).toBe('Use pnpm')
    expect(texts[1]!.text()).toBe(
      'Please also update the changelog and bump the version in package.json',
    )
    expect(texts[1]!.classes()).toContain('truncate')
    expect(texts[1]!.attributes('title')).toBe(long)
    expect(wrapper.get('[data-testid="composer-queue-sheet"]').attributes('aria-label')).toBe(
      'Queued messages',
    )
  })

  it('emits withdraw with the entry id from its ghost button', async () => {
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'first'), makeSteer('b', 'second')] },
    })

    const buttons = wrapper.findAll('[data-testid="composer-queue-withdraw"]')
    expect(buttons[0]!.attributes('aria-label')).toBe('Withdraw queued message')
    await buttons[1]!.trigger('click')

    expect(wrapper.emitted('withdraw')).toEqual([['b']])
  })

  it('removes a row when its entry leaves the queue', async () => {
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'first'), makeSteer('b', 'second')] },
    })

    await wrapper.setProps({ steers: [makeSteer('b', 'second')] })

    const texts = wrapper.findAll('[data-testid="composer-queue-text"]')
    expect(texts.map((node) => node.text())).toEqual(['second'])
  })

  it('is read-only as a peek edge and never takes focus', () => {
    const wrapper = mount(HarnessQueueSheet, {
      props: { steers: [makeSteer('a', 'first')], peek: true },
      attachTo: document.body,
    })

    expect(wrapper.find('[data-testid="composer-queue-withdraw"]').exists()).toBe(false)
    expect(document.activeElement).toBe(document.body)
    wrapper.unmount()
  })

  describe('scheduled messages', () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(NOW)
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    it('lists them after the queued steers with their time and a one-line preview', () => {
      const content = 'Check the nightly CI run\nand summarize failures'
      const wrapper = mount(HarnessQueueSheet, {
        props: {
          steers: [makeSteer('s', 'Use pnpm')],
          scheduled: [makeScheduled('a', { content })],
        },
        global: { stubs: menuStubs },
      })

      const rows = wrapper.findAll('li')
      expect(rows.map((row) => row.attributes('data-testid'))).toEqual([
        'composer-queue-row',
        'composer-scheduled-row',
      ])
      const time = wrapper.get('[data-testid="composer-scheduled-time"]')
      expect(time.text()).toBe(formatSendTime(TOMORROW_9))
      expect(time.text()).toMatch(/^Tomorrow, /)
      expect(time.classes()).toContain('tabular-nums')
      const text = wrapper.get('[data-testid="composer-scheduled-text"]')
      expect(text.text()).toBe('Check the nightly CI run and summarize failures')
      expect(text.attributes('title')).toBe(content)
    })

    it('renders the sheet when only scheduled messages exist', () => {
      const wrapper = mountScheduled([makeScheduled('a')])

      expect(wrapper.find('[data-testid="composer-queue-sheet"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="composer-queue-row"]').exists()).toBe(false)
      expect(wrapper.findAll('[data-testid="composer-scheduled-row"]')).toHaveLength(1)
    })

    it('emits send now and withdraw with the message id', async () => {
      const wrapper = mountScheduled([makeScheduled('a'), makeScheduled('b')])

      const sendNow = wrapper.findAll('[data-testid="composer-scheduled-send-now"]')
      expect(sendNow[0]!.attributes('aria-label')).toBe('Send now')
      await sendNow[1]!.trigger('click')
      const withdraw = wrapper.findAll('[data-testid="composer-scheduled-withdraw"]')
      expect(withdraw[0]!.attributes('aria-label')).toBe('Withdraw scheduled message')
      await withdraw[0]!.trigger('click')

      expect(wrapper.emitted('send-scheduled')).toEqual([['b']])
      expect(wrapper.emitted('withdraw-scheduled')).toEqual([['a']])
    })

    it('keeps withdraw but blocks send now while the chat takes no messages', () => {
      const wrapper = mountScheduled([makeScheduled('a')], { sendDisabled: true })

      expect(
        wrapper.get('[data-testid="composer-scheduled-send-now"]').attributes('disabled'),
      ).toBeDefined()
      expect(
        wrapper.get('[data-testid="composer-scheduled-withdraw"]').attributes('disabled'),
      ).toBeUndefined()
    })

    it('reschedules from the time menu', async () => {
      const wrapper = mountScheduled([makeScheduled('a'), makeScheduled('b')])
      const row = wrapper.findAll('[data-testid="composer-scheduled-row"]')[1]!

      expect(row.get('[data-testid="composer-scheduled-time"]').attributes('title')).toBe(
        'Reschedule',
      )
      await row.get('[data-testid="send-later-in-1-hour"]').trigger('click')

      expect(wrapper.emitted('reschedule-scheduled')).toEqual([
        ['b', new Date(2026, 10, 16, 15, 0)],
      ])
    })

    it('shows why a message was not sent and offers to try again', () => {
      const wrapper = mountScheduled([
        makeScheduled('a', { status: 'failed', error: 'Workspace is not running' }),
      ])

      const row = wrapper.get('[data-testid="composer-scheduled-row"]')
      expect(row.attributes('data-status')).toBe('failed')
      expect(row.find('.text-destructive').exists()).toBe(true)
      const error = row.get('[data-testid="composer-scheduled-error"]')
      expect(error.text()).toBe('Not sent · Workspace is not running')
      expect(error.attributes('title')).toBe('Workspace is not running')
      expect(row.get('[data-testid="composer-scheduled-send-now"]').attributes('aria-label')).toBe(
        'Try again now',
      )
      expect(row.find('[data-testid="composer-scheduled-withdraw"]').exists()).toBe(true)
      expect(row.find('[data-testid="send-later-menu"]').exists()).toBe(true)
    })

    it('shows a message being sent without actions', () => {
      const wrapper = mountScheduled([makeScheduled('a', { status: 'sending' })])

      const row = wrapper.get('[data-testid="composer-scheduled-row"]')
      expect(row.get('[data-testid="composer-scheduled-time"]').text()).toBe('Sending…')
      expect(row.find('.animate-spin').exists()).toBe(true)
      expect(row.find('button').exists()).toBe(false)
      expect(row.find('[data-testid="composer-scheduled-error"]').exists()).toBe(false)
    })

    it('is read-only as a peek edge', () => {
      const wrapper = mountScheduled([makeScheduled('a')], { peek: true })

      expect(wrapper.find('button').exists()).toBe(false)
      expect(wrapper.get('[data-testid="composer-scheduled-time"]').text()).toMatch(/^Tomorrow, /)
    })
  })

  it('only animates when motion is allowed', () => {
    const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(source)?.[1] ?? ''
    const guarded = /@media \(prefers-reduced-motion: no-preference\)\s*\{([\s\S]*?)\n\}\n/.exec(
      style,
    )?.[1]
    expect(guarded).toContain('animation:')
    expect(guarded).toContain('transition:')
    const outside = style.replace(guarded ?? '', '')
    expect(outside).not.toMatch(/\banimation\s*:/)
    expect(outside).not.toMatch(/\btransition\s*:/)
  })
})
