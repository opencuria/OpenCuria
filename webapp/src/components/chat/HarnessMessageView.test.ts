import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

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
  beforeEach(() => {
    setActivePinia(createPinia())
  })
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

  it('shows Thinking while streaming with no parts', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([]),
      },
    })

    expect(wrapper.get('[data-testid="harness-thinking"]').text()).toBe('Thinking')
    expect(wrapper.text()).not.toContain('Agent is thinking')
  })

  it('hides Thinking while a tool is running', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'tool-1',
            type: 'tool',
            state: 'running',
            tool: 'read',
            title: 'Read index.ts',
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Read index.ts')
  })

  it('hides Thinking while any of several tools is still running', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'tool-1',
            type: 'tool',
            state: 'running',
            tool: 'read',
            title: 'Read a.ts',
            output: '',
          }),
          makePart({
            id: 'tool-2',
            type: 'tool',
            state: 'running',
            tool: 'read',
            title: 'Read b.ts',
            output: '',
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="harness-worked-running"]').text()).toBe('2 running')
  })

  it('renders two running subtask cards at once', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'sub-1',
            type: 'subtask',
            state: 'running',
            title: 'Explore renderer',
            meta: { agent: 'explore', subtask_id: 'sub-1' },
          }),
          makePart({
            id: 'sub-2',
            type: 'subtask',
            state: 'running',
            title: 'General research',
            meta: { agent: 'general', subtask_id: 'sub-2' },
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="harness-subtask-row"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="harness-subtask-indicator"]').every((node) => node.attributes('data-running') === '1')).toBe(true)
  })

  it('shows Thinking again after tools complete while still streaming', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'tool-1',
            type: 'tool',
            state: 'completed',
            tool: 'read',
            title: 'Read index.ts',
          }),
        ]),
      },
    })

    expect(wrapper.get('[data-testid="harness-thinking"]').text()).toBe('Thinking')
    expect(wrapper.text()).toContain('Read index.ts')
  })

  it('hides Thinking while a subtask is running', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'sub-1',
            type: 'subtask',
            state: 'running',
            title: 'Find renderer',
            meta: { agent: 'explore', subtask_id: 'sub-1' },
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
  })

  it('does not render step-finish markers', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'Hello' }),
          makePart({
            id: 'step-1',
            type: 'step-finish',
            title: 'Step 1 finished',
            meta: { step: 1, cost: 0.01, tokens: { prompt_tokens: 10, completion_tokens: 4 } },
          }),
        ]),
      },
    })

    expect(wrapper.text()).not.toContain('Step 1 finished')
    expect(wrapper.find('[data-block-kind="step"]').exists()).toBe(false)
  })

  it('shows a hover usage footer on finished answers', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: {
          ...makeAssistant([makePart({ id: 't1', type: 'text', output: 'Done' })]),
          cost: 0.0123,
          tokens: { prompt: 1204, completion: 318, total: 1522 },
        },
      },
    })

    const footer = wrapper.get('[data-testid="harness-message-usage"]')
    expect(footer.text()).toBe('$0.0123 · 1,204 in · 318 out')
    expect(footer.classes()).toContain('opacity-0')
    expect(footer.classes()).toContain('group-hover:opacity-100')
  })

  it('hides the usage footer while streaming', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: {
          ...makeAssistant([makePart({ id: 't1', type: 'text', output: 'Done' })]),
          cost: 0.01,
          tokens: { prompt: 10, completion: 4, total: 14 },
        },
      },
    })

    expect(wrapper.find('[data-testid="harness-message-usage"]').exists()).toBe(false)
  })
})
