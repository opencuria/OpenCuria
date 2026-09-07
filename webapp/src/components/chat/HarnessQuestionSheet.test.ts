import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessQuestionSheet from './HarnessQuestionSheet.vue'
import type { HarnessQuestionRequest } from '@/types/harness'

function makeRequest(overrides: Partial<HarnessQuestionRequest> = {}): HarnessQuestionRequest {
  return {
    request_id: 'q-1',
    session_id: 'session-1',
    workspace_id: 'ws-1',
    questions: [
      {
        header: 'Setup',
        question: 'Which tool?',
        options: [
          { label: 'Option A', description: 'first' },
          { label: 'Option B', description: 'second' },
        ],
      },
      { question: 'Free text?' },
    ],
    ...overrides,
  }
}

describe('HarnessQuestionSheet', () => {
  it('shows a pager and letter-labeled options', () => {
    const wrapper = mount(HarnessQuestionSheet, {
      props: { requests: [makeRequest(), makeRequest({ request_id: 'q-2' })] },
    })

    expect(wrapper.find('[data-testid="composer-question-pager"]').text()).toContain('1 of 2')
    const options = wrapper.findAll('[data-testid="composer-question-option"]')
    expect(options).toHaveLength(2)
    expect(options[0]!.text()).toContain('A')
    expect(options[0]!.text()).toContain('Option A')
    expect(options[0]!.text()).toContain('first')
  })

  it('shows a source badge for subagent questions', () => {
    const wrapper = mount(HarnessQuestionSheet, {
      props: { requests: [makeRequest({ agent_name: 'explore' })] },
    })

    expect(wrapper.get('[data-testid="composer-question-source"]').text()).toBe('Explorer')
  })

  it('selects an option via keyboard letter and continues', async () => {
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })

    await wrapper.find('[data-testid="composer-question-sheet"]').trigger('keydown', { key: 'b' })
    const options = wrapper.findAll('[data-testid="composer-question-option"]')
    expect(options[1]!.attributes('data-variant')).toBe('default')

    await wrapper.find('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([['q-1', ['Option B', '']]])
  })

  it('skips via Escape and pages between requests', async () => {
    const wrapper = mount(HarnessQuestionSheet, {
      props: { requests: [makeRequest(), makeRequest({ request_id: 'q-2' })] },
    })

    await wrapper.find('[data-testid="composer-question-next"]').trigger('click')
    expect(wrapper.find('[data-testid="composer-question-pager"]').text()).toContain('2 of 2')

    await wrapper
      .find('[data-testid="composer-question-sheet"]')
      .trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('skip')).toEqual([['q-2']])
  })
})
