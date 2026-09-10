import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessMessageView from './HarnessMessageView.vue'
import type { HarnessMessage } from '@/types/harness'
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
    props: ['text', 'compact'],
    template: '<div class="markdown-stub">{{ text }}</div>',
  },
}))

function makeUser(overrides: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'user-1',
    session_id: 'session-1',
    role: 'user',
    content: 'original prompt',
    parts: [],
    ...overrides,
  }
}

function makeAssistant(overrides: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'assistant-1',
    session_id: 'session-1',
    role: 'assistant',
    content: 'answer',
    parts: [],
    ...overrides,
  }
}

describe('HarnessMessageView edit/fork', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetProviderCatalogCache()
  })

  it('shows edit and fork actions for persisted user messages', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(true)
  })

  it('hides edit/fork for assistant messages', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant() },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('hides edit/fork while disabled (busy run)', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser(), disabled: true },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('hides edit/fork for local optimistic messages without a backend row', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser({ id: 'local-user-session-1-123' }) },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('opens the editor on edit click and emits edit with the new text', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    const input = wrapper.get('[data-testid="message-edit-input"]')
    expect((input.element as HTMLTextAreaElement).value).toBe('original prompt')

    await input.setValue('edited prompt')
    await wrapper.get('[data-testid="message-edit-save"]').trigger('click')

    expect(wrapper.emitted('edit')).toEqual([['user-1', 'edited prompt']])
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(false)
  })

  it('does not emit edit when saving unchanged or blank text', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    await wrapper.get('[data-testid="message-edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toBeUndefined()

    const input = wrapper.get('[data-testid="message-edit-input"]')
    await input.setValue('   ')
    await wrapper.get('[data-testid="message-edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toBeUndefined()
  })

  it('closes the editor on cancel and on Escape', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(true)
    await wrapper.get('[data-testid="message-edit-cancel"]').trigger('click')
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(false)
    expect(wrapper.emitted('edit')).toBeUndefined()

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    const input = wrapper.get('[data-testid="message-edit-input"]')
    await input.setValue('draft changes')
    await input.trigger('keydown.escape')
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(false)
    expect(wrapper.emitted('edit')).toBeUndefined()
  })

  it('emits fork with the message id', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-fork"]').trigger('click')

    expect(wrapper.emitted('fork')).toEqual([['user-1']])
  })
})
