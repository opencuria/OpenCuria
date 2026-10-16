import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'

import HarnessQuestionSheet from './HarnessQuestionSheet.vue'
import type { HarnessQuestionRequest } from '@/types/harness'
import { useAuthStore } from '@/stores/auth'
import { loadQuestionInputCache, questionInputCacheKey } from '@/lib/questionInputCache'

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.user = { id: 1, email: 'test@example.test', first_name: '', last_name: '' }
  auth.activeOrganizationId = 'org-1'
})
afterEach(() => vi.restoreAllMocks())

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
    // First question has options → own-answer row with placeholder
    const ownInput = customs[0]!.element as HTMLInputElement
    expect(ownInput.placeholder).toContain('Own answer')
    // Second question is free-text only → standalone input with placeholder
    const freeInput = customs[1]!.element as HTMLInputElement
    expect(freeInput.placeholder).toContain('Your answer')
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

  it('hints single vs multi choice per question', () => {
    const single = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })
    expect(single.find('[data-testid="composer-question-hint"]').text()).toContain('Select one')
    expect(single.findAll('[data-testid="composer-question-option"]')[0]!.attributes('role')).toBe(
      'radio',
    )

    const multi = mount(HarnessQuestionSheet, {
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
    expect(multi.find('[data-testid="composer-question-hint"]').text()).toContain(
      'Select all that apply',
    )
    expect(multi.findAll('[data-testid="composer-question-option"]')[0]!.attributes('role')).toBe(
      'checkbox',
    )
  })

  it('keeps multiple option selections in single questions exclusive', async () => {
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })

    const options = wrapper.findAll('[data-testid="composer-question-option"]')
    await options[0]!.trigger('click')
    await options[1]!.trigger('click')
    await wrapper.find('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([['q-1', ['Option B', '']]])
  })
})

describe('question draft restoration', () => {
  it('saves every edit immediately and restores after unmount without submitting', async () => {
    const request = makeRequest()
    const key = questionInputCacheKey({ userId: 1, organizationId: 'org-1' }, request)
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [request] } })
    await wrapper.findAll('[data-testid="composer-question-option"]')[1]!.trigger('click')
    const input = wrapper.findAll('[data-testid="composer-question-custom"]')[1]!
    ;(input.element as HTMLInputElement).value = '  unfinished answer  '
    const edit = input.trigger('input')
    // No nextTick/unmount hook is required for the last keystroke to be safe.
    expect(loadQuestionInputCache(key, 2)).toEqual({
      answers: ['Option B', ''],
      custom: ['', '  unfinished answer  '],
    })
    await edit
    expect(wrapper.emitted('submit')).toBeUndefined()
    wrapper.unmount()

    const restored = mount(HarnessQuestionSheet, { props: { requests: [request] } })
    expect(
      restored.findAll('[data-testid="composer-question-option"]')[1]!.attributes('aria-checked'),
    ).toBe('true')
    expect(
      (restored.findAll('[data-testid="composer-question-custom"]')[1]!.element as HTMLInputElement)
        .value,
    ).toBe('  unfinished answer  ')
    await restored.get('[data-testid="composer-question-submit"]').trigger('click')
    expect(restored.emitted('submit')).toEqual([['q-1', ['Option B', 'unfinished answer']]])
    // Emitting alone is not a successful submission; only the store clears it.
    expect(loadQuestionInputCache(key, 2)).not.toBeNull()
    restored.unmount()
  })

  it('preserves multi-select and own answers through request list changes', async () => {
    const request = makeRequest({
      questions: [{ question: 'Pick', multiple: true, options: [{ label: 'A' }, { label: 'B' }] }],
    })
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [request] } })
    for (const option of wrapper.findAll('[data-testid="composer-question-option"]'))
      await option.trigger('click')
    await wrapper.get('[data-testid="composer-question-custom"]').setValue('extra')
    await wrapper.setProps({ requests: [] })
    await wrapper.setProps({ requests: [makeRequest({ request_id: 'other' })] })
    expect(
      (wrapper.findAll('[data-testid="composer-question-custom"]')[0]!.element as HTMLInputElement)
        .value,
    ).toBe('')
    await wrapper.setProps({ requests: [request] })
    await wrapper.get('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([['q-1', ['A\0B\0extra']]])
    wrapper.unmount()
  })

  it('isolates a reused request id across chat, workspace, user and organization changes', async () => {
    const request = makeRequest()
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [request] } })
    await wrapper.findAll('[data-testid="composer-question-custom"]')[1]!.setValue('original')
    for (const changed of [
      makeRequest({ session_id: 'other-session' }),
      makeRequest({ workspace_id: 'other-workspace' }),
    ]) {
      await wrapper.setProps({ requests: [changed] })
      expect(
        (
          wrapper.findAll('[data-testid="composer-question-custom"]')[1]!
            .element as HTMLInputElement
        ).value,
      ).toBe('')
    }
    await wrapper.setProps({ requests: [request] })
    const auth = useAuthStore()
    auth.activeOrganizationId = 'org-2'
    await wrapper.vm.$nextTick()
    expect(
      (wrapper.findAll('[data-testid="composer-question-custom"]')[1]!.element as HTMLInputElement)
        .value,
    ).toBe('')
    auth.activeOrganizationId = 'org-1'
    auth.user = { ...auth.user!, id: 2 }
    await wrapper.vm.$nextTick()
    expect(
      (wrapper.findAll('[data-testid="composer-question-custom"]')[1]!.element as HTMLInputElement)
        .value,
    ).toBe('')
    auth.user = { ...auth.user!, id: 1 }
    await wrapper.vm.$nextTick()
    expect(
      (wrapper.findAll('[data-testid="composer-question-custom"]')[1]!.element as HTMLInputElement)
        .value,
    ).toBe('original')
    wrapper.unmount()
  })

  it('keeps editing usable when storage writes fail', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const wrapper = mount(HarnessQuestionSheet, { props: { requests: [makeRequest()] } })
    await wrapper.findAll('[data-testid="composer-question-custom"]')[0]!.setValue('manual')
    await wrapper.get('[data-testid="composer-question-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([['q-1', ['manual', '']]])
    wrapper.unmount()
  })
})
