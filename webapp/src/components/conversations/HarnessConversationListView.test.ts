import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessConversationListView from '@/components/conversations/HarnessConversationListView.vue'
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
    mode: 'plan',
    agent_name: 'plan',
    model: '',
    unread: true,
    updated_at: '2026-03-29T10:00:00.000Z',
    ...overrides,
  }
}

describe('HarnessConversationListView', () => {
  it('renders conversation rows with workspace and mode labels', () => {
    const wrapper = mount(HarnessConversationListView, {
      props: {
        conversations: [makeConversation()],
        loading: false,
        searchQuery: '',
        formatTimeAgo: () => '2m ago',
      },
    })

    expect(wrapper.text()).toContain('Fix tests')
    expect(wrapper.text()).toContain('Workspace One')
    expect(wrapper.text()).toContain('Plan')
  })

  it('shows empty search state', () => {
    const wrapper = mount(HarnessConversationListView, {
      props: {
        conversations: [],
        loading: false,
        searchQuery: 'missing',
        formatTimeAgo: () => 'now',
      },
    })

    expect(wrapper.text()).toContain('No conversations match your search.')
  })
})
