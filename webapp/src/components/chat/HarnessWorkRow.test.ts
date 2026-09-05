import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessWorkRow from './HarnessWorkRow.vue'
import type { HarnessPart } from '@/types/harness'

vi.mock('@/components/common/LoadingSpinner.vue', () => ({
  default: { template: '<span class="loading-stub" />' },
}))

vi.mock('./HarnessMarkdown.vue', () => ({
  default: {
    props: ['text', 'compact'],
    template: '<div class="markdown-stub">{{ text }}</div>',
  },
}))

function makePart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-1',
    session_id: 'session-1',
    type: 'tool',
    state: 'completed',
    tool: 'read',
    title: 'Read index.ts',
    output: 'export const x = 1',
    ...overrides,
  }
}

describe('HarnessWorkRow', () => {
  it('labels reasoning as Thought', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({
          type: 'reasoning',
          title: '',
          tool: undefined,
          output: 'considering the layout',
        }),
      },
    })

    expect(wrapper.get('[data-testid="harness-work-row-label"]').text()).toBe('Thought')
    expect(wrapper.get('[data-testid="harness-work-row"]').attributes('data-expandable')).toBe(
      '1',
    )
  })

  it('expands a standalone tool row to show output', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: { part: makePart() },
    })

    expect(wrapper.text()).not.toContain('export const x = 1')
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.text()).toContain('export const x = 1')
    expect(wrapper.text()).toContain('read')
  })

  it('uses a tooltip instead of a collapsible for grouped completed tools', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: { part: makePart(), grouped: true },
    })

    expect(wrapper.find('[data-slot="collapsible-trigger"]').exists()).toBe(false)
    expect(wrapper.find('[data-slot="tooltip-trigger"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="harness-work-row"]').attributes('data-expandable')).toBe(
      '0',
    )
  })

  it('keeps grouped reasoning expandable', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        grouped: true,
        part: makePart({
          type: 'reasoning',
          title: '',
          output: 'need to check the store',
        }),
      },
    })

    expect(wrapper.get('[data-testid="harness-work-row-label"]').text()).toBe('Thought')
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.text()).toContain('need to check the store')
  })

  it('keeps grouped error tools expandable', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        grouped: true,
        part: makePart({
          state: 'error',
          title: 'Read missing.ts',
          output: 'file not found',
        }),
      },
    })

    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.text()).toContain('file not found')
  })

  it('shows a spinner while a tool is running', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({ state: 'running', output: '' }),
      },
    })

    expect(wrapper.find('.loading-stub').exists()).toBe(true)
  })

  it('uses Monitor for computer-use view tools', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({ tool: 'view_screen', title: 'View screen' }),
      },
    })

    expect(wrapper.find('[data-testid="harness-work-row-label"]').text()).toBe('View screen')
    expect(wrapper.find('svg.lucide-monitor').exists()).toBe(true)
  })

  it('uses MousePointer2 for computer-use input tools', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({ tool: 'left_click', title: 'Left click' }),
      },
    })

    expect(wrapper.find('svg.lucide-mouse-pointer-2').exists()).toBe(true)
  })
})
