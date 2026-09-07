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

  it('always shows a free-text input even when options exist', () => {
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })

    const customs = wrapper.findAll('[data-testid="composer-question-custom"]')
    expect(customs).toHaveLength(2)
    expect(wrapper.text()).toContain('Own answer')
    expect(wrapper.text()).toContain('Your answer')
  })

  it('submits custom text instead of a selected option', async () => {
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })

    await wrapper.findAll('[data-testid="composer-question-option"]')[0]!.trigger('click')
    await wrapper.findAll('[data-testid="composer-question-custom"]')[0]!.setValue('neither')
    await wrapper.find('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([['q-1', ['neither', '']]])
  })

  it('appends custom text to multi-select answers', async () => {
    const wrapper = mount(HarnessQuestionSheet, {
      props: {
        requests: [
          makeRequest({
            questions: [
              {
                question: 'Pick some',
                multiple: true,
                options: [
                  { label: 'Option A', description: 'first' },
                  { label: 'Option B', description: 'second' },
                ],
              },
            ],
          }),
        ],
      },
    })

    const options = wrapper.findAll('[data-testid="composer-question-option"]')
    await options[0]!.trigger('click')
    await options[1]!.trigger('click')
    await wrapper.get('[data-testid="composer-question-custom"]').setValue('extra')
    await wrapper.find('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([
      ['q-1', [`Option A${String.fromCharCode(0)}Option B${String.fromCharCode(0)}extra`]],
    ])
  })

  it('submits the selected option when custom text is empty', async () => {
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })

    await wrapper.findAll('[data-testid="composer-question-option"]')[0]!.trigger('click')
    await wrapper.find('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([['q-1', ['Option A', '']]])
  })
})
