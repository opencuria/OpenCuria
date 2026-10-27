import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessMessageView from './HarnessMessageView.vue'
import HarnessMessageFooter from './HarnessMessageFooter.vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listEngineModels: vi.fn().mockResolvedValue([]),
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
  const trigger = wrapper.find(
    '[data-testid="harness-worked-for"] [data-slot="collapsible-trigger"]',
  )
  if (trigger.exists()) await trigger.trigger('click')
}

describe('HarnessMessageView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetProviderCatalogCache()
  })
  it('keeps the newest streaming response block and cursor mounted beyond page one', async () => {
    const parts: HarnessPart[] = []
    for (let index = 0; index < 81; index += 1) {
      parts.push(
        makePart({
          id: `response-${index}`,
          type: 'text',
          output: `Response ${index}`,
        }),
      )
    }
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant(parts, { completed_at: null }), streaming: true },
    })

    expect(wrapper.find('[data-testid="harness-page-status"]').text()).toContain('Page 2 of 2')
    expect(wrapper.find('[data-part-id="response-80"] .animate-pulse').exists()).toBe(true)
  })

  it('paginates blocks while preserving order and bounds mounted rows', async () => {
    const parts = Array.from({ length: 161 }, (_, index) =>
      makePart({
        id: `steer-${index}`,
        type: 'steer',
        output: `Instruction ${index}`,
      }),
    )
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant(parts) },
    })

    expect(wrapper.get('[data-testid="harness-page-status"]').text()).toContain('Page 1 of 3')
    expect(wrapper.findAll('[data-block-kind="steer"]')).toHaveLength(80)
    expect(wrapper.find('[data-part-id="steer-79"]').exists()).toBe(true)
    expect(wrapper.find('[data-part-id="steer-80"]').exists()).toBe(false)

    await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    expect(wrapper.get('[data-testid="harness-page-status"]').text()).toContain('Page 2 of 3')
    expect(wrapper.findAll('[data-block-kind="steer"]')).toHaveLength(80)
    expect(wrapper.find('[data-part-id="steer-80"]').exists()).toBe(true)
    expect(wrapper.find('[data-part-id="steer-160"]').exists()).toBe(false)

    await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    expect(wrapper.get('[data-testid="harness-page-status"]').text()).toContain('Page 3 of 3')
    expect(wrapper.findAll('[data-block-kind="steer"]')).toHaveLength(1)
    expect(wrapper.find('[data-part-id="steer-160"]').exists()).toBe(true)
    expect(wrapper.find('[data-part-id="steer-159"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="harness-page-next"]').element.hasAttribute('disabled')).toBe(
      true,
    )
  })

  it('keeps live trailing text and cursor visible without losing the reader page on deltas', async () => {
    const steers = Array.from({ length: 161 }, (_, index) =>
      makePart({
        id: `steer-${index}`,
        type: 'steer',
        output: `Instruction ${index}`,
      }),
    )
    const initial = [...steers, makePart({ id: 'live-text-1', type: 'text', output: 'Answer' })]
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant(initial, { completed_at: null }), streaming: true },
    })

    expect(wrapper.get('[data-testid="harness-page-status"]').text()).toContain('Page 3 of 3')
    expect(wrapper.find('[data-part-id="live-text-1"] .animate-pulse').exists()).toBe(true)
    await wrapper.get('[data-testid="harness-page-previous"]').trigger('click')
    expect(wrapper.get('[data-testid="harness-page-status"]').text()).toContain('Page 2 of 3')
    expect(wrapper.findAll('[data-block-kind="steer"]')).toHaveLength(79)
    expect(wrapper.find('[data-part-id="live-text-1"] .animate-pulse').exists()).toBe(true)

    const firstVisibleSteer = wrapper.get('[data-part-id="steer-80"]').element
    const updated = [
      ...initial,
      makePart({ id: 'live-text-2', type: 'text', output: ' still live' }),
    ]
    await wrapper.setProps({ message: makeAssistant(updated, { completed_at: null }) })

    expect(wrapper.get('[data-testid="harness-page-status"]').text()).toContain('Page 2 of 3')
    expect(wrapper.get('[data-part-id="steer-80"]').element).toBe(firstVisibleSteer)
    expect(wrapper.find('[data-part-id="live-text-2"] .animate-pulse').exists()).toBe(true)
    expect(wrapper.findAll('[data-block-kind="steer"]')).toHaveLength(79)
    expect(wrapper.findAll('[data-block-kind]').length).toBeLessThanOrEqual(80)
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

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
    expect(kinds).toEqual(['workedFor', 'text'])
    expect(wrapper.get('[data-block-kind="workedFor"]').element.parentElement?.className).toContain(
      '[&>[data-block-kind=workedFor]]:-mb-0.5',
    )
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

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
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

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
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

  it('shows Thinking between streamed model steps', async () => {
    const start = makePart({ id: 'start', type: 'step-start', meta: { step: 1 } })
    const reasoning = makePart({
      id: 'reasoning',
      type: 'reasoning',
      state: 'running',
      output: 'Checking',
      meta: { step: 1 },
    })
    const wrapper = mount(HarnessMessageView, {
      props: { streaming: true, message: makeAssistant([start, reasoning]) },
    })
    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)

    const finish = makePart({ id: 'finish', type: 'step-finish', display: { step: 1 } })
    await wrapper.setProps({ message: makeAssistant([start, reasoning, finish]) })
    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(true)

    const next = makePart({ id: 'next', type: 'step-start', meta: { step: 2 } })
    await wrapper.setProps({ message: makeAssistant([start, reasoning, finish, next]) })
    expect(wrapper.find('[data-testid="harness-thinking"]').exists()).toBe(false)
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

    expect(
      wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['workedFor'])
    await expandWorkedFor(wrapper)
    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
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

  it('puts usage in the same footer as the answer actions', () => {
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
    const actions = wrapper.get('[data-testid="message-footer"]')
    expect(actions.element.contains(footer.element)).toBe(true)
    expect(actions.find('[data-testid="answer-copy"]').exists()).toBe(true)
    expect(actions.find('[data-testid="message-fork"]').exists()).toBe(true)
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

    expect(
      wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['workedFor'])
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
    expect(
      wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['workedFor', 'text'])
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

    expect(
      wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['workedFor', 'text'])
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

  it('keeps a leading compaction divider outside while interleaved compaction stays chronological inside', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({
            id: 'compact-0',
            type: 'compaction',
            title: 'Session compacted',
            output: 'older summary',
          }),
          makePart({ id: 't1', type: 'text', output: 'Starting' }),
          makePart({ id: 'tool-1', type: 'tool', tool: 'read', title: 'Read a.ts' }),
          makePart({
            id: 'compact-1',
            type: 'compaction',
            title: 'Session compacted',
            output: 'mid summary',
          }),
          makePart({ id: 'tool-2', type: 'tool', tool: 'grep', title: 'Grep foo' }),
          makePart({ id: 't2', type: 'text', output: 'Done' }),
        ]),
      },
    })

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
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
    expect(
      streaming.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['text', 'single', 'text'])

    const finished = mount(HarnessMessageView, { props: { message } })
    expect(
      finished.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['workedFor', 'text'])
    // Same finished message re-rendered (e.g. usage arrival) keeps one card.
    await finished.setProps({ message: { ...message } })
    expect(
      finished.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['workedFor', 'text'])
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

  it('shows the summary sentence quietly below the finished answer', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([makePart({ id: 't1', type: 'text', output: 'Fixed it.' })]),
        summary: 'Fix the login bug and add regression tests.',
      },
    })

    const summary = wrapper.get('[data-testid="message-summary"]')
    expect(summary.text()).toContain('Fix the login bug and add regression tests.')
    expect(summary.find('.sr-only').text()).toBe('Conversation summary:')
    const html = wrapper.html()
    expect(html.indexOf('Fixed it.')).toBeLessThan(html.indexOf('message-summary'))
    expect(html.indexOf('message-summary')).toBeLessThan(html.indexOf('message-footer'))
  })

  it('shows no summary line without a sentence or while the answer streams', () => {
    const message = makeAssistant([makePart({ id: 't1', type: 'text', output: 'Fixed it.' })])

    expect(
      mount(HarnessMessageView, { props: { message } })
        .find('[data-testid="message-summary"]')
        .exists(),
    ).toBe(false)
    expect(
      mount(HarnessMessageView, {
        props: { message, summary: 'Login fixed.', streaming: true },
      })
        .find('[data-testid="message-summary"]')
        .exists(),
    ).toBe(false)
  })

  it('keeps a delivered steer visible between two Worked for cards in chronological order', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        message: makeAssistant([
          makePart({ id: 't1', type: 'text', output: 'Looking around' }),
          makePart({ id: 'tool-1', type: 'tool', tool: 'read', title: 'Read index.ts' }),
          makePart({
            id: 'steer-1',
            type: 'steer',
            output: 'Use pnpm instead',
            meta: { steer_id: 's1' },
          }),
          makePart({ id: 't2', type: 'text', output: 'Switching now' }),
          makePart({ id: 'tool-2', type: 'tool', tool: 'bash', title: 'Run pnpm install' }),
          makePart({ id: 't3', type: 'text', output: 'All done' }),
        ]),
      },
    })

    const kinds = wrapper
      .findAll('[data-block-kind]')
      .map((node) => node.attributes('data-block-kind'))
      .filter((kind) => ['workedFor', 'steer', 'text'].includes(kind ?? ''))
    expect(kinds.slice(0, 3)).toEqual(['workedFor', 'steer', 'workedFor'])
    // The steer is a top-level row, not nested in a collapsed card.
    const steer = wrapper.get('[data-block-kind="steer"]')
    expect(steer.element.closest('[data-block-kind="workedFor"]')).toBeNull()
    expect(steer.text()).toContain('Use pnpm instead')
    expect(wrapper.findAll('[data-testid="harness-worked-for"]')).toHaveLength(2)
    // The footer copies only the closing answer, never the user's steer.
    expect(wrapper.findComponent(HarnessMessageFooter).props('copyText')).toBe('All done')
  })

  it('shows a steer inline while the turn is still streaming', () => {
    const wrapper = mount(HarnessMessageView, {
      props: {
        streaming: true,
        message: {
          ...makeAssistant([
            makePart({ id: 't1', type: 'text', output: 'Before' }),
            makePart({ id: 'steer-1', type: 'steer', output: 'Change course' }),
            makePart({ id: 't2', type: 'text', state: 'running', output: 'After' }),
          ]),
          completed_at: null,
        },
      },
    })

    expect(
      wrapper.findAll('[data-block-kind]').map((node) => node.attributes('data-block-kind')),
    ).toEqual(['text', 'steer', 'text'])
    expect(wrapper.get('[data-block-kind="steer"]').text()).toContain('Change course')
  })
})
