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
    expect(wrapper.find('svg.lucide-chevron-down').exists()).toBe(false)
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

  it('pages through 1000 grouped tools with at most 80 mounted rows', async () => {
    const parts = Array.from({ length: 1000 }, (_, index) =>
      makePart({ id: `tool-${index}`, title: `Read file-${index}.ts` }),
    )
    const wrapper = mount(HarnessWorkedGroup, { props: { parts } })

    // The collapsed group does not mount any individual tool rows.
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(0)
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(80)
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 1 of 13')
    expect(wrapper.findAll('[data-testid="harness-page-next"]')).toHaveLength(1)

    // Page forward to the middle; rows remain bounded and in input order.
    for (let index = 0; index < 6; index += 1) {
      await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    }
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 7 of 13')
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(80)
    expect(wrapper.find('[data-part-id="tool-480"]').exists()).toBe(true)

    // The final partial page reaches the last item and exposes a disabled Next.
    for (let index = 0; index < 6; index += 1) {
      await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    }
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 13 of 13')
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(40)
    expect(wrapper.find('[data-part-id="tool-999"]').exists()).toBe(true)
    expect(
      (wrapper.get('[data-testid="harness-page-next"]').element as HTMLButtonElement).disabled,
    ).toBe(true)
    await wrapper.get('[data-testid="harness-page-previous"]').trigger('click')
    expect(wrapper.find('[data-part-id="tool-880"]').exists()).toBe(true)
  }, 30000)

  it('keeps a growing live tool group on its newest page', async () => {
    const initial = Array.from({ length: 100 }, (_, index) =>
      makePart({ id: `tool-${index}`, title: `Read file-${index}.ts` }),
    )
    const wrapper = mount(HarnessWorkedGroup, { props: { parts: initial } })
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    await wrapper.get('[data-testid="harness-page-next"]').trigger('click')

    const grown = [
      ...initial,
      ...Array.from({ length: 1 }, (_, index) =>
        makePart({ id: `live-${index}`, title: `Read live-${index}.ts`, state: 'running' }),
      ),
    ]
    await wrapper.setProps({ parts: grown })

    expect(wrapper.find('[data-testid="harness-page-status"]').text()).toContain('Page 2 of 2')
    expect(wrapper.find('[data-part-id="live-0"]').exists()).toBe(true)
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
