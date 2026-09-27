import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessMessageView from './HarnessMessageView.vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listProviderModels: vi.fn().mockResolvedValue([]),
  }
})

vi.mock('@/components/common/LoadingSpinner.vue', () => ({
  default: { template: '<span class="loading-stub" />' },
}))

vi.mock('./HarnessMarkdown.vue', () => ({
  default: {
    props: ['text', 'compact', 'onPrimary'],
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

function makeAssistant(parts: HarnessPart[], extras: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'msg-1',
    session_id: 'session-1',
    role: 'assistant',
    content: parts
      .filter((part) => part.type === 'text')
      .map((part) => part.output)
      .join(''),
    parts,
    created_at: '2026-03-29T10:00:00.000Z',
    completed_at: '2026-03-29T10:05:11.000Z',
    ...extras,
  }
}

async function expandWorkedFor(wrapper: VueWrapper): Promise<void> {
  const trigger = wrapper.find('[data-testid="harness-worked-for"] [data-slot="collapsible-trigger"]')
  if (trigger.exists()) await trigger.trigger('click')
}

describe('HarnessMessageView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetProviderCatalogCache()
  })
  it('wraps initial text plus a single tool in chronological order inside one card', async () => {
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
    expect(kinds).toEqual(['workedFor', 'text'])
    expect(
      wrapper.get('[data-block-kind="workedFor"]').element.parentElement?.className,
    ).toContain('[&>[data-block-kind=workedFor]]:-mb-0.5')
    // Chronological order is preserved inside: initial text stays before the tool.
    await expandWorkedFor(wrapper)
    const inner = wrapper
      .get('[data-block-kind="workedFor"]')
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(inner).toEqual(['text', 'single'])
    const innerText = wrapper.get('[data-block-kind="workedFor"]').text()
    expect(innerText.indexOf('Hello')).toBeLessThan(innerText.indexOf('Read index.ts'))
    expect(wrapper.text()).toContain('Worked for 5m 11s')
    expect(wrapper.text()).toContain('Done')
  })

  it('wraps initial text plus consecutive work items under Worked inside one card', async () => {
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
    expect(kinds).toEqual(['workedFor', 'text'])
    await expandWorkedFor(wrapper)
    const inner = wrapper
      .get('[data-block-kind="workedFor"]')
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(inner).toEqual(['text', 'group'])
    expect(wrapper.text()).toContain('Looking around')
    expect(wrapper.text()).toContain('Worked for 5m 11s')
    expect(wrapper.text()).toContain('Found it')
    expect(wrapper.get('[data-testid="harness-worked-count"]').text()).toBe('2')
  })

  it('wraps a lone reasoning part in Worked for after the turn finishes', async () => {
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
    expect(kinds).toEqual(['workedFor'])
    expect(wrapper.text()).not.toContain('Thought')
    await expandWorkedFor(wrapper)
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

  it('keeps reasoning outside of a tool group inside Worked for', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'tool-1',
            type: 'tool',
            tool: 'read',
            title: 'Read a.ts',
          }),
          makePart({
            id: 'r1',
            type: 'reasoning',
            title: '',
            output: 'need a second file',
          }),
          makePart({
            id: 'tool-2',
            type: 'tool',
            tool: 'grep',
            title: 'Grep foo',
          }),
        ]),
      },
    })

    expect(wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'workedFor',
    ])
    await expandWorkedFor(wrapper)
    const kinds = wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['workedFor', 'single', 'single', 'single'])
    expect(wrapper.text()).toContain('Thought')
    expect(wrapper.find('[data-testid="harness-worked-group"]').exists()).toBe(false)
  })

  it('does not wrap tools while the turn is still streaming', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
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
            title: 'Grep foo',
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-worked-for"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="harness-worked-group"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="harness-worked-count"]').text()).toBe('2')
  })

  it('opens the matching child session for adjacent subtasks', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'sub-1',
            type: 'subtask',
            title: 'Explore renderer',
            meta: {
              agent: 'explore',
              subtask_id: 'sub-1',
              child_session_id: 'child-a',
            },
          }),
          makePart({
            id: 'sub-2',
            type: 'subtask',
            title: 'General research',
            meta: {
              agent: 'general',
              subtask_id: 'sub-2',
              child_session_id: 'child-b',
            },
          }),
        ]),
      },
    })

    await expandWorkedFor(wrapper)
    const rows = wrapper.findAll('[data-testid="harness-subtask-row"]')
    await rows[0]!.trigger('click')
    await rows[1]!.trigger('click')
    expect(wrapper.emitted('openSubtask')).toEqual([['child-a'], ['child-b']])
  })

  it('shows a patch card instead of the paired edit row while streaming', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'edit-1',
            type: 'tool',
            tool: 'edit',
            call_id: 'c1',
            title: 'Edit CommandPalette.vue',
          }),
          makePart({
            id: 'patch-1',
            type: 'patch',
            call_id: 'c1',
            title: 'Patch CommandPalette.vue',
            output: '--- a/a.ts\n+++ b/a.ts\n-old\n+new',
            meta: { path: '/workspace/CommandPalette.vue' },
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-worked-for"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="harness-patch-card"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('CommandPalette.vue')
    expect(wrapper.text()).not.toContain('Edit CommandPalette.vue')
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
    expect(
      wrapper
        .findAll('[data-testid="harness-subtask-indicator"]')
        .every((node) => node.attributes('data-running') === '1'),
    ).toBe(true)
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

  it('renders agent steps as a connected timeline, not a raw code block', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'start-1',
            type: 'step-start',
            title: 'Step 1',
            meta: { step: 1 },
          }),
          makePart({
            id: 'agent-1',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan one',
            meta: {
              step: 1,
              agent_meta: {
                verification: 'previous ok',
                analysis: 'login form',
                next_action: 'click submit',
                action: 'click "Submit"',
                action_kind: 'click',
              },
            },
          }),
          makePart({
            id: 'start-2',
            type: 'step-start',
            title: 'Step 2',
            meta: { step: 2 },
          }),
          makePart({
            id: 'agent-2',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan two',
            meta: {
              step: 2,
              agent_meta: {
                analysis: 'second screen',
                next_action: 'type hello',
                action: 'type',
                action_kind: 'type',
              },
            },
          }),
        ]),
      },
    })

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['agent', 'agent'])
    const steps = wrapper.findAll('[data-testid="harness-agent-step"]')
    expect(steps).toHaveLength(2)
    expect(steps[0]!.text()).toContain('Step 1')
    expect(steps[0]!.text()).toContain('Click "Submit"')
    expect(steps[1]!.text()).toContain('Step 2')
    expect(wrapper.text()).not.toContain('Step 1 finished')
    // Details stay collapsed: no raw monospace block up front.
    expect(wrapper.find('pre').exists()).toBe(false)
    expect(steps[0]!.attributes('data-status')).toBe('completed')
    expect(steps[1]!.attributes('data-part-id')).toBe('agent-2')
    const rails = wrapper.findAll('[data-testid="harness-agent-step-rail"]')
    expect(rails).toHaveLength(1)
    expect(rails[0]!.attributes('data-connected')).toBe('true')
  })

  it('does not connect the timeline rail across an interleaved text block', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'start-1',
            type: 'step-start',
            title: 'Step 1',
            meta: { step: 1 },
          }),
          makePart({
            id: 'agent-1',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan one',
            meta: { step: 1, agent_meta: { action: 'click', action_kind: 'click' } },
          }),
          makePart({ id: 't-gap', type: 'text', output: 'a note between steps' }),
          makePart({
            id: 'start-2',
            type: 'step-start',
            title: 'Step 2',
            meta: { step: 2 },
          }),
          makePart({
            id: 'agent-2',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan two',
            meta: { step: 2, agent_meta: { action: 'type', action_kind: 'type' } },
          }),
        ]),
      },
    })

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['agent', 'text', 'agent'])
    // Neither rail bridges the text gap: the first step is not connected to
    // the second, and the last step has no trailing rail at all.
    expect(wrapper.findAll('[data-testid="harness-agent-step-rail"]')).toHaveLength(0)
  })

  it('marks the last agent step failed on a final run-level error', () => {
    const parts = [
      makePart({
        id: 'start-1',
        type: 'step-start',
        title: 'Step 1',
        meta: { step: 1 },
      }),
      makePart({
        id: 'agent-1',
        type: 'agent',
        title: 'Agent plan',
        output: 'plan one',
        meta: { step: 1, agent_meta: { action: 'click', action_kind: 'click' } },
      }),
      makePart({
        id: 'finish-1',
        type: 'step-finish',
        title: 'Step 1 finished',
        meta: { step: 1 },
      }),
      makePart({
        id: 'start-2',
        type: 'step-start',
        title: 'Step 2',
        meta: { step: 2 },
      }),
      makePart({
        id: 'agent-2',
        type: 'agent',
        title: 'Agent plan',
        output: 'plan two',
        meta: { step: 2, agent_meta: { action: 'type', action_kind: 'type' } },
      }),
      makePart({
        id: 'finish-2',
        type: 'step-finish',
        title: 'Step 2 finished',
        meta: { step: 2 },
      }),
    ]
    const message = makeAssistant(parts)
    message.finish = 'error'
    message.error = 'action failed'
    const wrapper = mount(HarnessMessageView, { props: { message } })

    const statuses = wrapper.findAll('[data-testid="harness-agent-step-status"]')
    expect(statuses).toHaveLength(2)
    expect(statuses[0]!.text()).toBe('Completed')
    expect(statuses[1]!.text()).toBe('Failed')
  })

  it('keeps reasoning chronological and derives the step status from markers', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'start-1',
            type: 'step-start',
            title: 'Step 1',
            meta: { step: 1 },
          }),
          makePart({
            id: 'r1',
            type: 'reasoning',
            title: '',
            output: 'checking the screen',
            meta: { step: 1 },
          }),
          makePart({
            id: 'agent-1',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan one',
            meta: { step: 1, agent_meta: { action: 'click', action_kind: 'click' } },
          }),
        ]),
      },
    })

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['single', 'agent'])
    expect(wrapper.text()).toContain('Thought')
    expect(wrapper.text()).toContain('checking the screen')
    expect(wrapper.get('[data-testid="harness-agent-step-status"]').text()).toBe('In progress')
    // The step is still active: no extra generic Thinking row after the plan.
    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
  })

  it('hides Thinking while a later step sequence is still running', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: makeAssistant([
          makePart({
            id: 'start-1',
            type: 'step-start',
            title: 'Step 1',
            meta: { step: 1 },
          }),
          makePart({
            id: 'agent-1',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan one',
            meta: { step: 1 },
          }),
          makePart({
            id: 'finish-1',
            type: 'step-finish',
            title: 'Step 1 finished',
            meta: { step: 1 },
          }),
          makePart({
            id: 'start-2',
            type: 'step-start',
            title: 'Step 2',
            meta: { step: 2 },
          }),
          makePart({
            id: 'agent-2',
            type: 'agent',
            title: 'Agent plan',
            output: 'plan two',
            meta: { step: 2 },
          }),
          makePart({
            id: 'r2',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'working on step two',
            meta: { step: 2 },
          }),
        ]),
      },
    })

    const statuses = wrapper.findAll('[data-testid="harness-agent-step-status"]')
    expect(statuses).toHaveLength(2)
    expect(statuses[0]!.text()).toBe('Completed')
    expect(statuses[1]!.text()).toBe('In progress')
    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
  })

  it('falls back to raw plan text for legacy agent payloads', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'agent-legacy',
            type: 'agent',
            title: 'Agent plan',
            output: 'older free-form plan',
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-agent-step"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="harness-agent-step-action"]').text()).toBe(
      'older free-form plan',
    )
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('shows a hover usage footer on finished answers', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        models: [
          {
            id: 'acme/think',
            name: 'Think',
            reasoning_efforts: ['high'],
            default_effort: 'high',
            supports_tools: true,
            context_length: 1,
            max_output_tokens: 1,
          },
        ],
        message: {
          ...makeAssistant([makePart({ id: 't1', type: 'text', output: 'Done' })]),
          model: 'acme/think',
          reasoning_effort: 'high',
          cost: 0.0123,
          tokens: { prompt: 1204, completion: 318, total: 1522 },
        },
      },
    })

    const footer = wrapper.get('[data-testid="harness-message-usage"]')
    expect(footer.text()).toBe('Think · High · $0.0123 · 1,204 in · 318 out')
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

  it('shows a visible model line while streaming', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        models: [
          {
            id: 'acme/think',
            name: 'Think',
            reasoning_efforts: ['high'],
            default_effort: 'high',
            supports_tools: true,
            context_length: 1,
            max_output_tokens: 1,
          },
        ],
        message: {
          ...makeAssistant([makePart({ id: 't1', type: 'text', output: 'Working' })]),
          model: 'acme/think',
          reasoning_effort: 'high',
        },
      },
    })

    expect(wrapper.get('[data-testid="harness-message-running-model"]').text()).toBe('Think High')
    expect(wrapper.find('[data-testid="harness-message-usage"]').exists()).toBe(false)
  })

  it('shows the model on the hover footer when usage is empty', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        models: [],
        message: {
          ...makeAssistant([makePart({ id: 't1', type: 'text', output: 'Done' })]),
          model: 'unknown/model',
        },
      },
    })

    expect(wrapper.get('[data-testid="harness-message-usage"]').text()).toBe('unknown/model')
  })

  it('renders compaction as a collapsed divider, not a user bubble', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'Hello' }),
          makePart({
            id: 'compact-1',
            type: 'compaction',
            title: 'Session compacted',
            output: '## Objective\n- secret summary',
          }),
        ]),
      },
    })

    expect(wrapper.find('[data-testid="harness-compaction-divider"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Session compacted')
    expect(wrapper.text()).not.toContain('secret summary')
    expect(wrapper.find('pre').exists()).toBe(false)
    expect(
      wrapper.find('[data-testid="harness-compaction-divider"] svg.lucide-chevron-down').exists(),
    ).toBe(false)
  })

  it('shows an answered question card with its leading text inside one Worked for', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'One question' }),
          makePart({
            id: 'q-1',
            type: 'tool',
            tool: 'question',
            title: 'Which mode?',
            output: '{"answers":["Build"]}',
            input: { arguments: '{"questions":[{"question":"Which mode?"}]}' },
          }),
        ]),
      },
    })

    expect(wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'workedFor',
    ])
    // The card is hidden until Worked for is expanded, like patch cards.
    expect(wrapper.find('[data-testid="harness-question-card"]').exists()).toBe(false)
    await expandWorkedFor(wrapper)
    const card = wrapper.get('[data-testid="harness-question-card"]')
    expect(card.text()).toContain('Which mode?')
    expect(card.get('[data-testid="harness-question-card-answer"]').text()).toBe('Build')
    expect(wrapper.find('[data-testid="harness-worked-group"]').exists()).toBe(false)
    // Leading text keeps chronological order above the card.
    const innerText = wrapper.get('[data-block-kind="workedFor"]').text()
    expect(innerText.indexOf('One question')).toBeLessThan(innerText.indexOf('Which mode?'))
  })

  it('keeps text->tool->text->tool->text chronological inside one card with trailing text outside', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'First thought' }),
          makePart({ id: 'tool-1', type: 'tool', tool: 'read', title: 'Read a.ts' }),
          makePart({ id: 't2', type: 'text', output: 'Middle note' }),
          makePart({ id: 'tool-2', type: 'tool', tool: 'grep', title: 'Grep foo' }),
          makePart({ id: 't3', type: 'text', output: 'Final answer' }),
        ]),
      },
    })

    // Streaming stays flat (no card yet); the finished view wraps once.
    expect(wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'workedFor',
      'text',
    ])
    await expandWorkedFor(wrapper)
    const inner = wrapper
      .get('[data-block-kind="workedFor"]')
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(inner).toEqual(['text', 'single', 'text', 'single'])
    const text = wrapper.text()
    expect(text.indexOf('First thought')).toBeLessThan(text.indexOf('Read a.ts'))
    expect(text.indexOf('Read a.ts')).toBeLessThan(text.indexOf('Middle note'))
    expect(text.indexOf('Middle note')).toBeLessThan(text.indexOf('Grep foo'))
    expect(text.indexOf('Grep foo')).toBeLessThan(text.indexOf('Final answer'))
  })

  it('keeps subagent and patch adjacency chronological inside one card', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'Kicking off' }),
          makePart({
            id: 'sub-1',
            type: 'subtask',
            title: 'Explore renderer',
            meta: { agent: 'explore', subtask_id: 'sub-1', child_session_id: 'child-a' },
          }),
          makePart({ id: 'read-1', type: 'tool', tool: 'read', title: 'Read a.ts' }),
          makePart({
            id: 'patch-1',
            type: 'patch',
            call_id: 'c-other',
            title: 'Patch b.ts',
          }),
          makePart({ id: 't2', type: 'text', output: 'Finished' }),
        ]),
      },
    })

    expect(wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'workedFor',
      'text',
    ])
    await expandWorkedFor(wrapper)
    const inner = wrapper
      .get('[data-block-kind="workedFor"]')
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(inner).toEqual(['text', 'card', 'single', 'card'])
    const rows = wrapper.findAll('[data-testid="harness-subtask-row"]')
    expect(rows).toHaveLength(1)
    await rows[0]!.trigger('click')
    expect(wrapper.emitted('openSubtask')).toEqual([['child-a']])
  })

  it('keeps Agent-S plans chronological around tool work after idle', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 'agent-0', type: 'agent', title: 'Agent plan', output: 'plan zero' }),
          makePart({ id: 't1', type: 'text', output: 'Starting' }),
          makePart({ id: 'tool-1', type: 'tool', tool: 'read', title: 'Read a.ts' }),
          makePart({ id: 'agent-1', type: 'agent', title: 'Agent plan', output: 'plan one' }),
          makePart({ id: 'tool-2', type: 'tool', tool: 'grep', title: 'Grep foo' }),
          makePart({ id: 't2', type: 'text', output: 'Done' }),
        ]),
      },
    })

    // Agent-S plans are the computer-use timeline: both stay visible
    // outside while each response slice keeps one chronological card.
    expect(wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'agent',
      'workedFor',
      'agent',
      'workedFor',
      'text',
    ])
    expect(wrapper.findAll('[data-testid="harness-agent-step"]')).toHaveLength(2)
    await expandWorkedFor(wrapper)
    const cards = wrapper.findAll('[data-block-kind="workedFor"]')
    expect(cards).toHaveLength(2)
    const first = cards[0]!
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(first).toEqual(['text', 'single'])
    const text = cards[0]!.text()
    expect(text.indexOf('Starting')).toBeLessThan(text.indexOf('Read a.ts'))
  })

  it('keeps a leading compaction divider outside while interleaved compaction stays chronological inside', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 'compact-0', type: 'compaction', title: 'Session compacted', output: 'older summary' }),
          makePart({ id: 't1', type: 'text', output: 'Starting' }),
          makePart({ id: 'tool-1', type: 'tool', tool: 'read', title: 'Read a.ts' }),
          makePart({ id: 'compact-1', type: 'compaction', title: 'Session compacted', output: 'mid summary' }),
          makePart({ id: 'tool-2', type: 'tool', tool: 'grep', title: 'Grep foo' }),
          makePart({ id: 't2', type: 'text', output: 'Done' }),
        ]),
      },
    })

    const kinds = wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['compaction', 'workedFor', 'text'])
    expect(wrapper.findAll('[data-testid="harness-compaction-divider"]')).toHaveLength(1)
    await expandWorkedFor(wrapper)
    const inner = wrapper
      .get('[data-block-kind="workedFor"]')
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(inner).toContain('compaction')
  })

  it('keeps the streaming cursor on the trailing text while finished (stable card key)', async () => {
    const message = makeAssistant([
      makePart({ id: 't1', type: 'text', output: 'Working' }),
      makePart({ id: 'tool-1', type: 'tool', tool: 'read', title: 'Read a.ts' }),
      makePart({ id: 't2', type: 'text', output: 'Finishing' }),
    ])
    const streaming = mount(HarnessMessageView, {
      props: { streaming: true, message },
    })
    expect(streaming.find('[data-testid="harness-worked-for"]').exists()).toBe(false)
    expect(streaming.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'text',
      'single',
      'text',
    ])

    const finished = mount(HarnessMessageView, { props: { message } })
    expect(finished.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'workedFor',
      'text',
    ])
    // Same finished message re-rendered (e.g. usage arrival) keeps one card.
    await finished.setProps({ message: { ...message } })
    expect(finished.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind'))).toEqual([
      'workedFor',
      'text',
    ])
    expect(finished.findAll('[data-testid="harness-worked-for"]')).toHaveLength(1)
  })

  it('does not render message errors inline', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: {
          ...makeAssistant([makePart({ id: 't1', type: 'text', output: 'Hello' })]),
          error: 'aborted by user',
          finish: 'aborted',
        },
      },
    })

    expect(wrapper.text()).toContain('Hello')
    expect(wrapper.text()).not.toContain('aborted by user')
  })
})
