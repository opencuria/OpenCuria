import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessMessageView from './HarnessMessageView.vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'

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
    type: 'text',
    state: 'completed',
    title: '',
    output: '',
    ...overrides,
  }
}

function makeAssistant(parts: HarnessPart[]): HarnessMessage {
  return {
    id: 'msg-1',
    session_id: 'session-1',
    role: 'assistant',
    content: parts.filter((part) => part.type === 'text').map((part) => part.output).join(''),
    parts,
  }
}

describe('HarnessMessageView', () => {
  it('renders text and a single tool in chronological order', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'Hello' }),
          makePart({
            id: 'tool-1',
            type: 'tool',
            tool: 'read',
            title: 'Read index.ts',
            output: 'ok',
          }),
          makePart({ id: 't2', type: 'text', output: 'Done' }),
        ]),
      },
    })

    const kinds = wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['text', 'single', 'text'])
    expect(wrapper.text()).toContain('Hello')
    expect(wrapper.text()).toContain('Read index.ts')
    expect(wrapper.text()).toContain('Done')
    expect(wrapper.find('[data-testid="harness-worked-group"]').exists()).toBe(false)
  })

  it('groups consecutive work items under Worked', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'Looking around' }),
          makePart({
            id: 'tool-1',
            type: 'tool',
            tool: 'read',
            title: 'Read index.ts',
          }),
          makePart({
            id: 'tool-2',
            type: 'tool',
            tool: 'grep',
            title: 'Grep RouteMeta',
          }),
          makePart({ id: 't2', type: 'text', output: 'Found it' }),
        ]),
      },
    })

    const kinds = wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['text', 'group', 'text'])
    expect(wrapper.text()).toContain('Looking around')
    expect(wrapper.text()).toContain('Worked')
    expect(wrapper.text()).toContain('Found it')
    expect(wrapper.find('[data-testid="harness-worked-group"]').exists()).toBe(true)
  })

  it('keeps a lone reasoning part at the top level', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'r1',
            type: 'reasoning',
            title: '',
            output: 'planning the change',
          }),
        ]),
      },
    })

    const kinds = wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['single'])
    expect(wrapper.text()).toContain('Thought')
    expect(wrapper.find('[data-testid="harness-worked-group"]').exists()).toBe(false)
  })

  it('shows the thinking placeholder while streaming with no parts', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([]),
      },
    })

    expect(wrapper.text()).toContain('Agent is thinking')
  })
})
