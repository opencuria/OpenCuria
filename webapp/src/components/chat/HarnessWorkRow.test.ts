import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessWorkRow from './HarnessWorkRow.vue'
import type { HarnessPart } from '@/types/harness'
import { requestHarnessPartDetailKey } from '@/lib/harnessPartDetail'

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
  it('does not request detail until an unopened grouped row expands', async () => {
    const request = vi.fn().mockResolvedValue(true)
    const wrapper = mount(HarnessWorkRow, {
      props: { grouped: true, part: makePart({ detail_loaded: false, output: '' }) },
      global: { provide: { [requestHarnessPartDetailKey as symbol]: request } },
    })
    expect(request).not.toHaveBeenCalled()
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(request).toHaveBeenCalledWith('session-1', 'part-1')
  })

  it('labels reasoning as Thought and shows a one-line preview', () => {
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
    expect(wrapper.get('[data-testid="harness-work-row-preview"]').text()).toBe(
      'considering the layout',
    )
    expect(wrapper.get('[data-testid="harness-work-row"]').attributes('data-expandable')).toBe('1')
  })

  it('previews the last reasoning summary line', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({
          type: 'reasoning',
          title: '',
          tool: undefined,
          output:
            'Planning sequential subagent implementation\n\nDefining session recording feature details',
        }),
      },
    })

    expect(wrapper.get('[data-testid="harness-work-row-preview"]').text()).toBe(
      'Defining session recording feature details',
    )
  })

  it('expands a standalone tool row to show the tool detail', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: { part: makePart() },
    })

    expect(wrapper.text()).not.toContain('export const x = 1')
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.get('[data-testid="tool-detail-read"]').text()).toContain('export const x = 1')
  })

  it('keeps grouped completed tools expandable', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: { part: makePart(), grouped: true },
    })

    expect(wrapper.find('[data-slot="collapsible-trigger"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="harness-work-row"]').attributes('data-expandable')).toBe('1')
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.get('[data-testid="tool-detail-read"]').text()).toContain('export const x = 1')
  })

  it('expands grouped reasoning to markdown', async () => {
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

  it('keeps error tools collapsed until the user explicitly expands them', async () => {
    const request = vi.fn().mockResolvedValue(true)
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({
          state: 'error',
          title: 'Read missing.ts',
          output: 'Tool failed',
          detail_loaded: false,
          display: { tool: 'read', summary: 'Read missing.ts' },
        }),
      },
      global: { provide: { [requestHarnessPartDetailKey as symbol]: request } },
    })

    expect(wrapper.find('[data-testid="tool-detail-read"]').exists()).toBe(false)
    expect(request).not.toHaveBeenCalled()
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(request).toHaveBeenCalledWith('session-1', 'part-1')
  })

  it('keeps bash rows collapsed even when the command failed', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({
          tool: 'bash',
          state: 'error',
          title: '$ missing',
          output: 'Command exited with code 127',
          input: { arguments: '{"command":"missing"}' },
        }),
      },
    })

    expect(wrapper.find('[data-testid="tool-detail-bash"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="harness-work-row-label"]').text()).toContain('$ missing')
  })

  it('shimmers the label while a tool is running', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({ state: 'running', output: '' }),
      },
    })

    expect(wrapper.get('[data-testid="harness-work-row-label"]').classes()).toContain(
      'harness-shimmer',
    )
    expect(wrapper.get('[data-slot="collapsible-trigger"]').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('[data-testid="harness-work-row"]').attributes('data-state')).toBe('running')
  })

  it('stops shimmering once the tool is done', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({ state: 'completed' }),
      },
    })

    expect(wrapper.get('[data-testid="harness-work-row-label"]').classes()).not.toContain(
      'harness-shimmer',
    )
    expect(wrapper.get('[data-slot="collapsible-trigger"]').attributes('aria-busy')).toBeUndefined()
  })

  it('splits file tools into verb, file name and directory hint', () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({ tool: 'read', title: 'Read src/components/App.vue' }),
      },
    })

    expect(wrapper.get('[data-testid="harness-work-row-label"]').text()).toBe('Read App.vue')
    expect(wrapper.get('[data-testid="harness-work-row-hint"]').text()).toBe('src/components')
    expect(wrapper.get('[data-slot="collapsible-trigger"]').attributes('title')).toBe(
      'Read src/components/App.vue',
    )
  })

  it('renders Computer Use MCP calls with a monitor and dedicated details', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({
          tool: 'mcp_computer-use_cua_repl_js',
          title: '',
          input: { arguments: { code: 'await cua.getState()', title: 'Inspect desktop' } },
        }),
      },
    })

    expect(wrapper.find('[data-testid="harness-work-row-label"]').text()).toBe('Inspect desktop')
    expect(wrapper.find('svg.lucide-monitor').exists()).toBe(true)
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.get('[data-testid="tool-detail-computer-use"]').text()).toBe('Inspect desktop')
  })

  it('uses native icons for the new built-in tools', () => {
    const expectedIcons = [
      ['websearch', 'search'],
      ['memory', 'book-text'],
      ['image_generation', 'image'],
      ['tool_call', 'wrench'],
    ] as const

    for (const [tool, icon] of expectedIcons) {
      const wrapper = mount(HarnessWorkRow, { props: { part: makePart({ tool }) } })
      expect(wrapper.find(`svg.lucide-${icon}`).exists()).toBe(true)
      wrapper.unmount()
    }
  })

  it('selects dedicated memory and image generation details and generic native details', async () => {
    const cases = [
      {
        part: makePart({ tool: 'memory', output: 'memory output' }),
        detail: 'tool-detail-memory',
      },
      {
        part: makePart({ tool: 'image_generation', output: 'image output' }),
        detail: 'tool-detail-image-generation',
      },
      {
        part: makePart({ tool: 'websearch', output: 'search output' }),
        detail: 'tool-detail-default',
      },
      {
        part: makePart({ tool: 'tool_call', output: 'call output' }),
        detail: 'tool-detail-default',
      },
    ]

    for (const testCase of cases) {
      const wrapper = mount(HarnessWorkRow, { props: { part: testCase.part } })
      await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
      expect(wrapper.find(`[data-testid="${testCase.detail}"]`).exists()).toBe(true)
      wrapper.unmount()
    }
  })

  it('renders a bash terminal detail', async () => {
    const wrapper = mount(HarnessWorkRow, {
      props: {
        part: makePart({
          tool: 'bash',
          title: '$ ls',
          output: 'a.ts',
          input: { arguments: '{"command":"ls"}' },
          meta: { exit_code: 0 },
        }),
      },
    })

    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.get('[data-testid="tool-detail-bash"]').text()).toContain('$ ls')
    expect(wrapper.get('[data-testid="tool-detail-bash-exit"]').text()).toContain('exit 0')
  })
})
