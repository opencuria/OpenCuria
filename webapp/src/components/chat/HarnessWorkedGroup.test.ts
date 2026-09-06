import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessWorkedGroup from './HarnessWorkedGroup.vue'
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
    output: 'ok',
    ...overrides,
  }
}

describe('HarnessWorkedGroup', () => {
  const completedParts: HarnessPart[] = [
    makePart({ id: 'tool-1', title: 'Read index.ts' }),
    makePart({ id: 'tool-2', tool: 'grep', title: 'Grep RouteMeta' }),
  ]

  it('renders a Worked header with the work-item count', () => {
    const wrapper = mount(HarnessWorkedGroup, {
      props: { parts: completedParts },
    })

    expect(wrapper.text()).toContain('Worked')
    expect(wrapper.get('[data-testid="harness-worked-count"]').text()).toBe('2')
  })

  it('stays collapsed by default when no part is running', () => {
    const wrapper = mount(HarnessWorkedGroup, {
      props: { parts: completedParts },
    })

    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(0)
  })

  it('expands to list compact rows', async () => {
    const wrapper = mount(HarnessWorkedGroup, {
      props: { parts: completedParts },
    })

    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    const rows = wrapper.findAll('[data-testid="harness-work-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('Read index.ts')
    expect(rows[1]!.text()).toContain('Grep RouteMeta')
  })

  it('stays collapsed while running and shows the live tool title', () => {
    const wrapper = mount(HarnessWorkedGroup, {
      props: {
        parts: [
          makePart({ id: 'tool-1', title: 'Read index.ts' }),
          makePart({
            id: 'tool-2',
            tool: 'grep',
            title: 'Grep foo',
            state: 'running',
            output: '',
          }),
        ],
      },
    })

    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(0)
    expect(wrapper.get('[data-testid="harness-worked-live"]').text()).toBe('Grep foo')
    expect(wrapper.find('.loading-stub').exists()).toBe(true)
  })

  it('shows a running count when several parts run at once', () => {
    const wrapper = mount(HarnessWorkedGroup, {
      props: {
        parts: [
          makePart({
            id: 'tool-1',
            title: 'Read a.ts',
            state: 'running',
            output: '',
          }),
          makePart({
            id: 'tool-2',
            tool: 'grep',
            title: 'Grep foo',
            state: 'running',
            output: '',
          }),
        ],
      },
    })

    expect(wrapper.get('[data-testid="harness-worked-running"]').text()).toBe('2 running')
    expect(wrapper.get('[data-testid="harness-worked-live"]').text()).toBe('Grep foo')
  })

  it('does not render step-finish rows inside the group', async () => {
    const wrapper = mount(HarnessWorkedGroup, {
      props: {
        parts: [
          makePart({ id: 'tool-1', title: 'Read index.ts' }),
          makePart({
            id: 'step-1',
            type: 'step-finish',
            title: 'Step 1 finished',
            tool: undefined,
          }),
          makePart({ id: 'tool-2', tool: 'grep', title: 'Grep foo' }),
        ],
      },
    })

    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.text()).not.toContain('Step 1 finished')
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(2)
  })
})
