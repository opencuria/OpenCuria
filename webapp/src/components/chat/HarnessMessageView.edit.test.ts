import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import HarnessMessageView from './HarnessMessageView.vue'
import type { HarnessMessage } from '@/types/harness'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'
import { useSkillStore } from '@/stores/skills'

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
    template:
      '<div class="markdown-stub" :data-on-primary="String(onPrimary ?? false)">{{ text }}</div>',
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

function seedSkills(): void {
  useSkillStore().skills = [
    {
      id: 'skill-1',
      name: 'Lint rules',
      body: 'Always lint',
      scope: 'personal',
      created_by_email: null,
      created_at: '2026-03-29T10:00:00.000Z',
      updated_at: '2026-03-29T10:00:00.000Z',
    },
  ]
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

  it('shows edit but no fork for persisted user messages', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('shows fork but no edit under assistant messages', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant() },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(
      wrapper.get('[data-testid="message-footer"]').find('[data-testid="message-fork"]').exists(),
    ).toBe(true)
  })

  it.each([{ disabled: true }, { streaming: true }])('hides assistant fork for %o', (state) => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant(), ...state },
    })
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

  it('emits fork with the assistant message id', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant() },
    })

    await wrapper.get('[data-testid="message-fork"]').trigger('click')

    expect(wrapper.emitted('fork')).toEqual([['assistant-1']])
  })

  it('renders the user bubble as a neutral surface without an avatar', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    expect(wrapper.find('.bg-primary\\/10').exists()).toBe(false)
    expect(wrapper.find('.markdown-stub').attributes('data-on-primary')).toBe('false')
    const bubble = wrapper.get('[data-testid="harness-user-bubble"]')
    expect(bubble.classes()).toContain('bg-muted/60')
    expect(bubble.classes()).not.toContain('bg-primary')
  })

  it('turns the bubble into an inline editor card with a focused input', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
      attachTo: document.body,
    })

    expect(wrapper.find('[data-testid="message-edit-card"]').exists()).toBe(false)
    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    await nextTick()

    const card = wrapper.get('[data-testid="message-edit-card"]')
    expect(card.find('[data-testid="message-edit-input"]').exists()).toBe(true)
    expect(card.find('[data-testid="message-edit-cancel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="harness-user-bubble"]').exists()).toBe(false)
    const input = wrapper.get('[data-testid="message-edit-input"]').element as HTMLTextAreaElement
    expect(document.activeElement).toBe(input)
    expect(input.selectionStart).toBe(input.value.length)
    wrapper.unmount()
  })

  it('saves on Enter but not on Shift+Enter or during IME composition', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    const input = wrapper.get('[data-testid="message-edit-input"]')
    await input.setValue('edited prompt')

    await input.trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(wrapper.emitted('edit')).toBeUndefined()

    await input.trigger('keydown', { key: 'Enter', isComposing: true })
    expect(wrapper.emitted('edit')).toBeUndefined()

    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('edit')).toEqual([['user-1', 'edited prompt']])
  })

  it('shows no skill pills without skill_ids', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    expect(wrapper.find('[data-testid="message-skills"]').exists()).toBe(false)
  })

  it('shows no skill pills on assistant messages', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeAssistant({ skill_ids: ['skill-1'] }) },
    })

    expect(wrapper.find('[data-testid="message-skills"]').exists()).toBe(false)
  })

  it('renders skill pills with resolved names, composer style', () => {
    seedSkills()
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser({ skill_ids: ['skill-1'] }) },
    })

    const pills = wrapper.findAll('[data-testid="message-skill-pill"]')
    expect(wrapper.find('[data-testid="message-skills"]').exists()).toBe(true)
    expect(pills).toHaveLength(1)
    expect(pills[0]!.text()).toContain('Lint rules')
    expect(pills[0]!.classes()).toContain('bg-primary/10')
    expect(pills[0]!.classes()).toContain('text-primary')
  })

  it('falls back to the short id for unknown skills', () => {
    seedSkills()
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser({ skill_ids: ['deadbeef-1234-5678-9abc-def012345678'] }) },
    })

    expect(wrapper.get('[data-testid="message-skill-pill"]').text()).toContain('deadbeef')
  })
})
