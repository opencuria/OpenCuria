import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessConversationKanbanView from '@/components/conversations/HarnessConversationKanbanView.vue'
import type { HarnessConversation } from '@/types/harness'

vi.mock('@/components/workspaces/StartNewHarnessChatDialog.vue', () => ({
  default: {
    name: 'StartNewHarnessChatDialog',
    template: '<div><slot name="trigger" /></div>',
  },
}))

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 'session-1',
    workspace_id: 'ws-1',
    workspace_name: 'Workspace One',
    title: 'Fix tests',
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    model: 'acme/think',
    reasoning_effort: 'high',
    unread: false,
    updated_at: '2026-03-29T10:00:00.000Z',
    ...overrides,
  }
}

describe('HarnessConversationKanbanView', () => {
  it('renders the three kanban columns with counts', () => {
    const wrapper = mount(HarnessConversationKanbanView, {
      props: {
        idleConvs: [makeConversation()],
        workingConvs: [makeConversation({ session_id: 'session-2', status: 'busy' })],
        doneConvs: [
          makeConversation({ session_id: 'session-3', unread: true, status: 'idle' }),
        ],
        formatTimeAgo: () => '1m ago',
        formatModelEffort: (conv) =>
          `${conv.model} ${conv.reasoning_effort ?? ''}`.trim(),
      },
    })

    expect(wrapper.text()).toContain('Available')
    expect(wrapper.text()).toContain('In Progress')
    expect(wrapper.text()).toContain('Done')
    expect(wrapper.text()).toContain('Fix tests')
    expect(wrapper.text()).toContain('acme/think high')
    expect(wrapper.text()).toContain('Build')
  })

  it('emits conversationClick when a card is clicked', async () => {
    const conv = makeConversation({ session_id: 'session-busy', status: 'busy' })
    const wrapper = mount(HarnessConversationKanbanView, {
      props: {
        idleConvs: [],
        workingConvs: [conv],
        doneConvs: [],
        formatTimeAgo: () => 'now',
        formatModelEffort: (conv) =>
          `${conv.model} ${conv.reasoning_effort ?? ''}`.trim(),
      },
    })

    const buttons = wrapper.findAll('button').filter((button) =>
      button.text().includes('Fix tests'),
    )
    expect(buttons.length).toBeGreaterThan(0)
    await buttons[0]!.trigger('click')
    expect(wrapper.emitted('conversationClick')?.[0]?.[0]).toEqual(conv)
  })

  it('keeps done cards from shrinking so the column can scroll', () => {
    const wrapper = mount(HarnessConversationKanbanView, {
      props: {
        idleConvs: [],
        workingConvs: [],
        doneConvs: [
          makeConversation({ session_id: 'done-1', title: 'Done one', unread: true }),
          makeConversation({ session_id: 'done-2', title: 'Done two', unread: true }),
          makeConversation({ session_id: 'done-3', title: 'Done three', unread: true }),
        ],
        formatTimeAgo: () => '1m ago',
        formatModelEffort: (conv) =>
          `${conv.model} ${conv.reasoning_effort ?? ''}`.trim(),
      },
    })

    const doneCards = wrapper.findAll('button').filter((button) =>
      button.text().includes('Done '),
    )
    expect(doneCards).toHaveLength(3)
    for (const card of doneCards) {
      expect(card.classes()).toContain('shrink-0')
    }
  })
})
