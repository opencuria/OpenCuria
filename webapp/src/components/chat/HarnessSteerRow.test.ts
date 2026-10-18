import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

import HarnessSteerRow from './HarnessSteerRow.vue'
import HarnessMessageView from './HarnessMessageView.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import type { HarnessMessage, HarnessPart } from '@/types/harness'

vi.mock('@/services/socket', () => ({ sendFilesRead: vi.fn() }))

function makeSteerPart(output: string): HarnessPart {
  return {
    id: 'steer-part-1',
    session_id: 'session-1',
    type: 'steer',
    state: 'completed',
    title: '',
    output,
    meta: { steer_id: 'steer-1' },
  }
}

function mountRow(part: HarnessPart) {
  setActivePinia(createPinia())
  return mount(HarnessSteerRow, {
    props: { part },
    global: { provide: { [harnessWorkspaceIdKey as symbol]: ref('ws-1') } },
  })
}

describe('HarnessSteerRow', () => {
  it('renders the steered text as a right-aligned user bubble with a muted caption', () => {
    const wrapper = mountRow(makeSteerPart('Use **pnpm** instead'))

    const row = wrapper.get('[data-testid="harness-steer-row"]')
    expect(row.classes()).toContain('justify-end')
    expect(wrapper.get('[data-testid="harness-user-bubble"]').text()).toContain('Use pnpm instead')
    const caption = wrapper.get('[data-testid="harness-steer-caption"]')
    expect(caption.text()).toBe('Sent while working')
    expect(caption.classes()).toContain('text-muted-foreground')
    expect(caption.find('svg').exists()).toBe(true)
  })

  it('shares the exact bubble classes of a regular user message', () => {
    const steer = mountRow(makeSteerPart('hello'))
    const user: HarnessMessage = {
      id: 'user-1',
      session_id: 'session-1',
      role: 'user',
      content: 'hello',
      parts: [],
    }
    const message = mount(HarnessMessageView, {
      props: { message: user },
      global: { provide: { [harnessWorkspaceIdKey as symbol]: ref('ws-1') } },
    })

    const steerBubble = steer.get('[data-testid="harness-user-bubble"]')
    const userBubble = message.get('[data-testid="harness-user-bubble"]')
    expect(steerBubble.classes()).toEqual(userBubble.classes())
    expect(steerBubble.classes()).toContain('bg-primary')
  })

  it('offers no edit, fork or copy actions', () => {
    const wrapper = mountRow(makeSteerPart('hello'))
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
  })

  it('renders mention badges and image thumbnails like a user message', async () => {
    const wrapper = mountRow(
      makeSteerPart('Fix @file:/workspace/src/a.ts see @file:/workspace/cat.png'),
    )
    await new Promise((resolve) => setTimeout(resolve, 60))

    expect(wrapper.findAll('[data-testid="mention-badge-file"]').length).toBeGreaterThanOrEqual(1)
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="mention-images"]').classes()).toContain('justify-end')
  })
})
