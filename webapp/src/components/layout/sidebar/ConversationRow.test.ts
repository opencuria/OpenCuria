import { describe, expect, it, vi } from 'vitest'
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
    template: '<button type="button" @click="$emit(\'click\')"><slot /></button>',
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
    ...overrides,
  }
}

function mountRow(overrides: Partial<HarnessConversation> = {}, props: Record<string, unknown> = {}) {
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

  it('shows a spinner instead of an unread dot while busy', () => {
    const wrapper = mountRow({ status: 'busy', unread: true })

    expect(wrapper.find('[data-testid="busy-spinner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="unread-dot"]').exists()).toBe(false)
  })

  it('emits select on click', async () => {
    const wrapper = mountRow()

    await wrapper.get('[data-testid="conversation-row"]').trigger('click')

    expect(wrapper.emitted('select')?.[0]?.[0]).toMatchObject({ session_id: 's-1' })
  })

  it('renames via the row menu and confirms with Enter', async () => {
    const wrapper = mountRow()

    const rename = wrapper.findAll('button').find((button) => button.text().includes('Umbenennen'))
    expect(rename).toBeTruthy()
    await rename!.trigger('click')

    const input = wrapper.get('[data-testid="conversation-rename-input"]')
    await input.setValue('Renamed chat')
    await input.trigger('keydown.enter')

    expect(wrapper.emitted('rename')?.[0]?.[1]).toBe('Renamed chat')
  })

  it('cancels rename on Escape', async () => {
    const wrapper = mountRow()

    const rename = wrapper.findAll('button').find((button) => button.text().includes('Umbenennen'))
    await rename!.trigger('click')
    const input = wrapper.get('[data-testid="conversation-rename-input"]')
    await input.setValue('Nope')
    await input.trigger('keydown.esc')

    expect(wrapper.find('[data-testid="conversation-row"]').exists()).toBe(true)
    expect(wrapper.emitted('rename')).toBeUndefined()
  })

  it('emits delete from the row menu', async () => {
    const wrapper = mountRow()

    const remove = wrapper.findAll('button').find((button) => button.text().includes('Löschen'))
    await remove!.trigger('click')

    expect(wrapper.emitted('delete')?.[0]?.[0]).toMatchObject({ session_id: 's-1' })
  })

  it('shows the workspace name instead of relative time when asked', () => {
    const wrapper = mountRow({}, { showWorkspace: true })

    expect(wrapper.get('[data-testid="conversation-row"]').text()).toContain('Alpha')
  })
})
