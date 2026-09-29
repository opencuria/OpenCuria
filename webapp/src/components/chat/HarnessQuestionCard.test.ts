import { describe, expect, it, vi } from 'vitest'
import { requestHarnessPartDetailKey } from '@/lib/harnessPartDetail'
import { mount } from '@vue/test-utils'

import HarnessQuestionCard from './HarnessQuestionCard.vue'
import type { HarnessPart } from '@/types/harness'

function makePart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'q-1',
    session_id: 'session-1',
    type: 'tool',
    state: 'completed',
    tool: 'question',
    title: 'Which mode?',
    output: '',
    ...overrides,
  }
}

function answeredPart(): HarnessPart {
  return makePart({
    output: '{"answers":["Build"]}',
    input: {
      arguments: JSON.stringify({ questions: [{ question: 'Which mode?' }] }),
    },
  })
}

describe('HarnessQuestionCard', () => {
  it('pairs the question with its answer', () => {
    const wrapper = mount(HarnessQuestionCard, { props: { part: answeredPart() } })

    expect(wrapper.get('[data-testid="harness-question-card"]').text()).toContain('Which mode?')
    expect(wrapper.get('[data-testid="harness-question-card-answer"]').text()).toBe('Build')
    expect(wrapper.find('[data-testid="harness-question-card-status"]').exists()).toBe(false)
  })

  it('numbers multiple questions and shows the count', () => {
    const wrapper = mount(HarnessQuestionCard, {
      props: {
        part: makePart({
          output: '{"answers":["A1","A2"]}',
          input: {
            arguments: JSON.stringify({
              questions: [{ question: 'First?' }, { question: 'Second?' }],
            }),
          },
        }),
      },
    })

    expect(wrapper.text()).toContain('2 questions')
    expect(wrapper.text()).toContain('First?')
    expect(wrapper.text()).toContain('Second?')
    expect(wrapper.findAll('[data-testid="harness-question-card-answer"]')).toHaveLength(2)
  })

  it('shows a placeholder for missing answers', () => {
    const wrapper = mount(HarnessQuestionCard, {
      props: {
        part: makePart({
          output: '{}',
          input: {
            arguments: JSON.stringify({ questions: [{ question: 'Which mode?' }] }),
          },
        }),
      },
    })

    expect(wrapper.text()).toContain('Which mode?')
    expect(wrapper.text()).toContain('No answer')
  })

  it('marks a rejected question as skipped', () => {
    const wrapper = mount(HarnessQuestionCard, {
      props: {
        part: makePart({
          state: 'error',
          output: "Tool 'question' failed: Question rejected by user",
          input: {
            arguments: JSON.stringify({ questions: [{ question: 'Which mode?' }] }),
          },
        }),
      },
    })

    expect(wrapper.get('[data-testid="harness-question-card-status"]').text()).toBe('Skipped')
    expect(wrapper.text()).toContain('Which mode?')
  })

  it('renders bounded live question rows and generic error status without opening detail', () => {
    const request = vi.fn().mockResolvedValue(true)
    const wrapper = mount(HarnessQuestionCard, {
      props: {
        part: makePart({
          state: 'error',
          output: 'Tool failed',
          detail_loaded: false,
          display: { tool: 'question', question_rows: [{ question: 'Continue?', answer: '' }] },
          input: { tool: 'question' },
        }),
      },
      global: { provide: { [requestHarnessPartDetailKey as symbol]: request } },
    })
    expect(wrapper.text()).toContain('Continue?')
    expect(wrapper.get('[data-testid="harness-question-card-status"]').text()).toBe('Failed')
    expect(request).not.toHaveBeenCalled()
  })

  it('uses the full fetched question detail after the compact preview hydrates', () => {
    const wrapper = mount(HarnessQuestionCard, {
      props: {
        part: makePart({
          detail_loaded: true,
          display: { tool: 'question', question_rows: [{ question: 'Short?', answer: 'yes' }] },
          input: {
            arguments: JSON.stringify({ questions: [{ question: 'The complete question?' }] }),
          },
          output: '{"answers":["A detailed answer"]}',
        }),
      },
    })
    expect(wrapper.text()).toContain('The complete question?')
    expect(wrapper.text()).toContain('A detailed answer')
    expect(wrapper.text()).not.toContain('Short?')
  })

  it('marks a timed-out question as timed out', () => {
    const wrapper = mount(HarnessQuestionCard, {
      props: {
        part: makePart({
          state: 'error',
          output: "Tool 'question' failed: Question timed out after 30s",
          input: {
            arguments: JSON.stringify({ questions: [{ question: 'Which mode?' }] }),
          },
        }),
      },
    })

    expect(wrapper.get('[data-testid="harness-question-card-status"]').text()).toBe('Timed out')
  })
})
