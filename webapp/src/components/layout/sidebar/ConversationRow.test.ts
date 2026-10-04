import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import ConversationRow from './ConversationRow.vue'
import type { HarnessConversation } from '@/types/harness'

const dropdownStubs = {
  Tooltip: { template: '<div><slot /></div>' },
  TooltipContent: { template: '<div><slot /></div>' },
  TooltipTrigger: { template: '<div><slot /></div>' },
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: {
    props: ['disabled'],
    emits: ['click'],
    template: '<button type="button" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
  },
}

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 's-1',
    workspace_id: 'ws-1',
    workspace_name: 'Alpha',
    title: 'First chat',
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    model: '',
    unread: false,
    updated_at: new Date().toISOString(),
    last_message_at: new Date().toISOString(),
    ...overrides,
  }
}

function mountRow(
  overrides: Partial<HarnessConversation> = {},
  props: Record<string, unknown> = {},
) {
  return mount(ConversationRow, {
    props: { conversation: makeConversation(overrides), ...props },
    global: { stubs: dropdownStubs },
  })
}

describe('ConversationRow', () => {
  it('renders the title and marks the active row', () => {
    const wrapper = mountRow({}, { active: true })

    expect(wrapper.get('[data-testid="conversation-row"]').text()).toContain('First chat')
    expect(wrapper.get('[data-testid="conversation-row"]').classes()).toContain('bg-primary/10')
  })

  it('shows an unread dot for unread idle chats', () => {
    const wrapper = mountRow({ unread: true })

    expect(wrapper.find('[data-testid="unread-dot"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="busy-spinner"]').exists()).toBe(false)
  })

  it.each([
    ['plan', 'build', 'lucide-list-todo', 'bg-primary/5'],
    ['build', 'plan', 'lucide-hammer', 'bg-emerald-500/10'],
  ] as const)(
    'uses the %s agent symbol only in the inbox, independently of mode',
    (agent_name, mode, iconClass, background) => {
      const wrapper = mountRow(
        { unread: true, agent_name, mode },
        { inbox: true, showWorkspace: true },
      )
      const icon = wrapper.get('[data-testid="inbox-result-icon"]')
      expect(icon.attributes('data-kind')).toBe(agent_name)
      expect(icon.classes()).toContain(iconClass)
      expect(wrapper.get('[data-testid="conversation-row"]').classes()).toContain(background)
      expect(wrapper.get('[data-testid="conversation-row"]').attributes('aria-label')).toBe(
        `Open chat First chat — Unread ${agent_name} response`,
      )
      expect(wrapper.find('[data-testid="inbox-result-edge"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="unread-dot"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="conversation-row-meta"]').text()).toContain('Alpha')
      expect(
        mountRow({ unread: true, agent_name, mode })
          .find('[data-testid="inbox-result-icon"]')
          .exists(),
      ).toBe(false)
    },
  )

  it.each(['question', 'permission', 'both'] as const)(
    'prioritizes %s gates over the inbox result symbol',
    (attention_kind) => {
      const wrapper = mountRow(
        { unread: true, needs_attention: true, attention_kind },
        { inbox: true },
      )
      expect(wrapper.find('[data-testid="attention-icon"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="attention-edge"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="inbox-result-icon"]').exists()).toBe(false)
    },
  )

  it('does not label busy or read inbox rows as plan/build results', () => {
    for (const overrides of [{ unread: false }, { unread: true, status: 'busy' as const }]) {
      const wrapper = mountRow(overrides, { inbox: true })
      expect(wrapper.find('[data-testid="inbox-result-icon"]').exists()).toBe(false)
    }
  })

  it('updates the inbox icon when the agent changes and uses a stronger active background', async () => {
    const wrapper = mountRow({ unread: true }, { inbox: true, active: true })
    expect(wrapper.get('[data-testid="conversation-row"]').classes()).toContain('bg-emerald-500/20')
    await wrapper.setProps({
      conversation: makeConversation({ unread: true, agent_name: 'plan', mode: 'plan' }),
    })
    expect(wrapper.get('[data-testid="inbox-result-icon"]').attributes('data-kind')).toBe('plan')
    expect(wrapper.get('[data-testid="conversation-row"]').classes()).toContain('bg-primary/15')
  })

  it('shows a spinner instead of an unread dot while busy', () => {
    const wrapper = mountRow({ status: 'busy', unread: true })

    expect(wrapper.find('[data-testid="busy-spinner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="unread-dot"]').exists()).toBe(false)
  })

  it('shows an attention icon instead of the busy spinner', () => {
    const wrapper = mountRow({
      status: 'busy',
      needs_attention: true,
      attention_kind: 'permission',
    })

    expect(wrapper.find('[data-testid="attention-icon"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="busy-spinner"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="conversation-row"]').text()).toContain('First chat')
  })

  it('uses a question icon for question gates', () => {
    const wrapper = mountRow({
      needs_attention: true,
      attention_kind: 'question',
    })

    expect(wrapper.find('[data-testid="attention-icon"]').exists()).toBe(true)
    expect(wrapper.get('[aria-label="Open chat First chat — Question waiting"]')).toBeTruthy()
  })

  it('emits mark-unread from the row menu when the chat is read', async () => {
    const wrapper = mountRow({ unread: false })

    const unread = wrapper
      .findAll('button')
      .find((button) => button.text().includes('Mark as unread'))
    expect(unread).toBeTruthy()
    await unread!.trigger('click')

    expect(wrapper.emitted('mark-unread')?.[0]?.[0]).toMatchObject({ session_id: 's-1' })
  })

  it('emits mark-read from the row menu when the chat is unread', async () => {
    const wrapper = mountRow({ unread: true })

    const read = wrapper.findAll('button').find((button) => button.text().includes('Mark as read'))
    expect(read).toBeTruthy()
    await read!.trigger('click')

    expect(wrapper.emitted('mark-read')?.[0]?.[0]).toMatchObject({ session_id: 's-1' })
  })

  it('emits select on click', async () => {
    const wrapper = mountRow()

    await wrapper.get('[data-testid="conversation-row"]').trigger('click')

    expect(wrapper.emitted('select')?.[0]?.[0]).toMatchObject({ session_id: 's-1' })
  })

  it('shows relative time from last_message_at, not updated_at', () => {
    const wrapper = mountRow({
      last_message_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
      updated_at: new Date().toISOString(),
    })

    expect(wrapper.get('[data-testid="conversation-row-meta"]').text()).toContain('5m')
  })

  it('renames via the row menu and confirms with Enter', async () => {
    const wrapper = mountRow()

    const rename = wrapper.findAll('button').find((button) => button.text().includes('Rename'))
    expect(rename).toBeTruthy()
    await rename!.trigger('click')

    const input = wrapper.get('[data-testid="conversation-rename-input"]')
    await input.setValue('Renamed chat')
    await input.trigger('keydown.enter')

    expect(wrapper.emitted('rename')?.[0]?.[1]).toBe('Renamed chat')
  })

  it('disables mutations but keeps navigation and read state available', async () => {
    const wrapper = mountRow({}, { actionsDisabled: true })
    const rename = wrapper.findAll('button').find((button) => button.text() === 'Rename')!
    const remove = wrapper.findAll('button').find((button) => button.text() === 'Delete')!

    expect(rename.attributes('disabled')).toBeDefined()
    expect(remove.attributes('disabled')).toBeDefined()
    await rename.trigger('click')
    await remove.trigger('click')
    expect(wrapper.find('[data-testid="conversation-rename-input"]').exists()).toBe(false)
    expect(wrapper.emitted('delete')).toBeUndefined()

    await wrapper.get('[data-testid="conversation-row"]').trigger('click')
    await wrapper.get('[data-testid="conversation-row"]').trigger('keydown.enter')
    expect(wrapper.emitted('select')).toHaveLength(2)
    await wrapper.get('[data-testid="mark-unread-item"]').trigger('click')
    expect(wrapper.emitted('mark-unread')).toHaveLength(1)
  })

  it('closes an in-progress rename when workspace actions become disabled', async () => {
    const wrapper = mountRow()
    await wrapper.findAll('button').find((button) => button.text() === 'Rename')!.trigger('click')
    await wrapper.get('[data-testid="conversation-rename-input"]').setValue('Renamed')
    await wrapper.setProps({ actionsDisabled: true })

    expect(wrapper.find('[data-testid="conversation-rename-input"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="conversation-row"]').exists()).toBe(true)
    expect(wrapper.emitted('rename')).toBeUndefined()
  })

  it('cancels rename on Escape', async () => {
    const wrapper = mountRow()

    const rename = wrapper.findAll('button').find((button) => button.text().includes('Rename'))
    await rename!.trigger('click')
    const input = wrapper.get('[data-testid="conversation-rename-input"]')
    await input.setValue('Nope')
    await input.trigger('keydown.esc')

    expect(wrapper.find('[data-testid="conversation-row"]').exists()).toBe(true)
    expect(wrapper.emitted('rename')).toBeUndefined()
  })

  it('emits delete from the row menu', async () => {
    const wrapper = mountRow()

    const remove = wrapper.findAll('button').find((button) => button.text().includes('Delete'))
    await remove!.trigger('click')

    expect(wrapper.emitted('delete')?.[0]?.[0]).toMatchObject({ session_id: 's-1' })
  })

  it('shows the workspace name instead of relative time when asked', () => {
    const wrapper = mountRow({}, { showWorkspace: true })

    expect(wrapper.get('[data-testid="conversation-row"]').text()).toContain('Alpha')
  })

  it('sizes the trailing meta to its content so titles can use leftover space', () => {
    const wrapper = mountRow({ title: 'Bitte um eine Frage auf Wunsch' })
    const meta = wrapper.get('[data-testid="conversation-row-meta"]')

    expect(meta.classes()).toContain('min-w-6')
    expect(meta.classes()).not.toContain('w-20')
    expect(wrapper.get('[data-testid="conversation-row"]').text()).toContain(
      'Bitte um eine Frage auf Wunsch',
    )
  })

  it('truncates long workspace names in the trailing meta', () => {
    const wrapper = mountRow(
      { workspace_name: 'A very long workspace name' },
      { showWorkspace: true },
    )
    const metaLabel = wrapper.get('[data-testid="conversation-row-meta"] span')

    expect(metaLabel.classes()).toContain('truncate')
    expect(metaLabel.classes()).toContain('max-w-16')
  })
})
