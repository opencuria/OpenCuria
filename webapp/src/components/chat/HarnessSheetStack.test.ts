import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessSheetStack from './HarnessSheetStack.vue'
import type { ComposerSheet } from '@/lib/composerSheets'
import type { HarnessTodo } from '@/types/harness'

vi.mock('@/components/chat/HarnessMentionSheet.vue', () => ({
  default: {
    props: ['candidates', 'activeIndex'],
    template: '<div data-testid="mention-stub">{{ candidates.length }}:{{ activeIndex }}</div>',
  },
}))

vi.mock('@/components/chat/HarnessQuestionSheet.vue', () => ({
  default: {
    props: ['requests', 'submitting'],
    template: '<div data-testid="question-stub">{{ requests.length }}</div>',
  },
}))

vi.mock('@/components/chat/HarnessPermissionSheet.vue', () => ({
  default: {
    props: ['request', 'resolving'],
    template: '<div data-testid="permission-stub">{{ request.tool }}</div>',
  },
}))

vi.mock('@/components/chat/HarnessTodoSheet.vue', () => ({
  default: {
    props: ['todos', 'open'],
    template: '<div data-testid="todo-stub">{{ todos.length }}</div>',
  },
}))

function makeTodo(id: string, overrides: Partial<HarnessTodo> = {}): HarnessTodo {
  return { id, content: id, status: 'pending', priority: 'medium', order: 0, ...overrides }
}

function makeSheets(): ComposerSheet[] {
  return [
    {
      kind: 'mention',
      mention: {
        candidates: [{ kind: 'file', label: 'a.ts', insert: 'file:a.ts' }],
        activeIndex: 0,
      },
    },
    {
      kind: 'question',
      question: {
        request_id: 'q-1',
        session_id: 's-1',
        workspace_id: 'ws-1',
        questions: [{ question: 'Which?' }],
      },
      questions: [
        {
          request_id: 'q-1',
          session_id: 's-1',
          workspace_id: 'ws-1',
          questions: [{ question: 'Which?' }],
        },
      ],
    },
    {
      kind: 'permission',
      permission: {
        request_id: 'p-1',
        session_id: 's-1',
        workspace_id: 'ws-1',
        tool: 'bash',
        pattern: 'ls',
        title: 'Run ls',
      },
    },
    { kind: 'todos', todos: [makeTodo('t1')] },
  ]
}

describe('HarnessSheetStack', () => {
  it('renders nothing when no sheet is active', () => {
    const wrapper = mount(HarnessSheetStack, { props: { sheets: [] } })
    expect(wrapper.find('[data-testid="composer-sheet-stack"]').exists()).toBe(false)
  })

  it('insets the stack so it docks inside the input card corners', () => {
    const wrapper = mount(HarnessSheetStack, { props: { sheets: makeSheets() } })
    const stack = wrapper.find('[data-testid="composer-sheet-stack"]')
    expect(stack.classes()).toContain('mx-6')
    expect(stack.classes()).toContain('sm:mx-7')
    expect(stack.classes()).toContain('-mb-px')
    expect(stack.classes()).not.toContain('max-w-3xl')
    expect(stack.classes()).not.toContain('mx-auto')
  })

  it('renders the topmost sheet interactively and lower sheets as peek edges', () => {
    const wrapper = mount(HarnessSheetStack, { props: { sheets: makeSheets() } })

    expect(wrapper.find('[data-testid="composer-sheet-top"]').attributes('data-sheet-kind')).toBe(
      'mention',
    )
    expect(wrapper.find('[data-testid="mention-stub"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="question-stub"]').exists()).toBe(false)

    const peeks = wrapper.findAll('[data-testid="composer-sheet-peek"]')
    expect(peeks).toHaveLength(3)
    for (const peek of peeks) {
      expect(peek.attributes('aria-hidden')).toBe('true')
      expect(peek.classes()).toContain('pointer-events-none')
    }
  })

  it('forwards mention selection and hover events', async () => {
    const wrapper = mount(HarnessSheetStack, { props: { sheets: makeSheets() } })
    const stubs = wrapper.findAllComponents({ name: 'HarnessMentionSheet' })
    expect(stubs).toHaveLength(1)
    const mention = stubs[0]!
    const candidate = { kind: 'file', label: 'a.ts', insert: 'file:a.ts' } as const
    mention.vm.$emit('select', candidate)
    mention.vm.$emit('hover', 2)

    expect(wrapper.emitted('mention-select')).toEqual([[candidate]])
    expect(wrapper.emitted('mention-hover')).toEqual([[2]])
  })

  it('forwards question submit/skip and permission resolve events', async () => {
    const wrapper = mount(HarnessSheetStack, {
      props: {
        sheets: [makeSheets()[1]!, makeSheets()[2]!],
      },
    })
    const questions = wrapper.findAllComponents({ name: 'HarnessQuestionSheet' })
    expect(questions).toHaveLength(1)
    const question = questions[0]!
    question.vm.$emit('submit', 'q-1', ['a'])
    question.vm.$emit('skip', 'q-1')

    expect(wrapper.emitted('question-submit')).toEqual([['q-1', ['a']]])
    expect(wrapper.emitted('question-skip')).toEqual([['q-1']])
  })
})
