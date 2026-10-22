import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import HarnessMessageView from './HarnessMessageView.vue'
import ModelPicker from '@/components/common/ModelPicker.vue'
import type { HarnessMessage, HarnessSession } from '@/types/harness'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'
import { useHarnessStore } from '@/stores/harness'
import { useSkillStore } from '@/stores/skills'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listEngineModels: vi.fn().mockResolvedValue([
      { id: 'acme/think', name: 'Think', provider: 'acme', reasoning_efforts: ['low', 'high'] },
      { id: 'acme/other', name: 'Other', provider: 'acme', reasoning_efforts: ['low', 'high'] },
    ]),
    listRecentModels: vi.fn().mockResolvedValue([]),
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

function seedSession(overrides: Partial<HarnessSession> = {}): void {
  useHarnessStore().sessions = [
    {
      id: 'session-1',
      workspace_id: 'ws-1',
      parent_id: null,
      title: 'Chat',
      mode: 'build',
      agent_name: 'build',
      model: 'acme/think',
      reasoning_effort: 'high',
      status: 'idle',
      cost: 0,
      tokens: {},
      ...overrides,
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

  it('hides edit/fork while disabled (read-only or subagent)', () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser(), disabled: true },
    })

    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('keeps edit but hides fork while the session is busy', () => {
    const user = mount(HarnessMessageView, { props: { message: makeUser(), busy: true } })
    const assistant = mount(HarnessMessageView, {
      props: { message: makeAssistant(), busy: true },
    })

    expect(user.find('[data-testid="message-edit"]').exists()).toBe(true)
    expect(assistant.find('[data-testid="message-fork"]').exists()).toBe(false)
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

    expect(wrapper.emitted('edit')).toEqual([
      ['user-1', 'edited prompt', { model: '', effort: '' }],
    ])
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(false)
  })

  it('reruns an unchanged prompt but never blank text', async () => {
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    const input = wrapper.get('[data-testid="message-edit-input"]')
    await input.setValue('   ')
    expect(wrapper.get('[data-testid="message-edit-save"]').attributes('disabled')).toBeDefined()
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('edit')).toBeUndefined()

    await input.setValue('original prompt')
    const save = wrapper.get('[data-testid="message-edit-save"]')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    expect(wrapper.emitted('edit')).toEqual([
      ['user-1', 'original prompt', { model: '', effort: '' }],
    ])
  })

  it('preselects the session model and forwards a changed model/effort', async () => {
    seedSession()
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser() },
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    await flushPromises()
    const picker = wrapper.get('[data-testid="message-edit-card"]').findComponent(ModelPicker)
    expect(picker.props('model')).toBe('acme/think')
    expect(picker.props('effort')).toBe('high')
    expect(picker.props('models').map((model: { id: string }) => model.id)).toEqual([
      'acme/think',
      'acme/other',
    ])

    picker.vm.$emit('update:model', 'acme/other')
    picker.vm.$emit('update:effort', 'low')
    await nextTick()
    await wrapper.get('[data-testid="message-edit-save"]').trigger('click')

    expect(wrapper.emitted('edit')).toEqual([
      ['user-1', 'original prompt', { model: 'acme/other', effort: 'low' }],
    ])
  })

  it('asks before stopping a busy session and keeps editing on cancel', async () => {
    seedSession({ status: 'busy' })
    const wrapper = mount(HarnessMessageView, {
      props: { message: makeUser(), busy: true },
      attachTo: document.body,
    })

    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    await wrapper.get('[data-testid="message-edit-input"]').setValue('edited prompt')
    await wrapper.get('[data-testid="message-edit-save"]').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('edit')).toBeUndefined()
    const cancel = document.querySelector<HTMLElement>(
      '[data-testid="message-edit-confirm-cancel"]',
    )
    expect(document.querySelector('[data-testid="message-edit-confirm"]')?.textContent).toContain(
      'Stop the running session?',
    )
    cancel?.click()
    await flushPromises()
    expect(document.querySelector('[data-testid="message-edit-confirm"]')).toBeNull()
    expect(wrapper.emitted('edit')).toBeUndefined()
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(true)

    await wrapper.get('[data-testid="message-edit-input"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    document.querySelector<HTMLElement>('[data-testid="message-edit-confirm-rerun"]')?.click()
    await flushPromises()

    expect(wrapper.emitted('edit')).toEqual([
      ['user-1', 'edited prompt', { model: 'acme/think', effort: 'high' }],
    ])
    expect(wrapper.find('[data-testid="message-edit-input"]').exists()).toBe(false)
    wrapper.unmount()
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
    expect(wrapper.emitted('edit')).toEqual([
      ['user-1', 'edited prompt', { model: '', effort: '' }],
    ])
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
