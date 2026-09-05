import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessSubtaskCard from './HarnessSubtaskCard.vue'
import { useHarnessStore } from '@/stores/harness'
import type { HarnessPart } from '@/types/harness'

vi.mock('@/components/common/LoadingSpinner.vue', () => ({
  default: { template: '<span class="loading-stub" />' },
}))

function makeSubtaskPart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-sub-1',
    message_id: 'msg-1',
    session_id: 'session-parent',
    type: 'subtask',
    state: 'completed',
    title: 'research the codebase',
    output: 'found it',
    meta: { subtask_id: 'sub-1', agent: 'explore' },
    ...overrides,
  }
}

describe('HarnessSubtaskCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders the subtask title and agent badge', () => {
    const wrapper = mount(HarnessSubtaskCard, {
      props: { part: makeSubtaskPart() },
    })

    expect(wrapper.text()).toContain('research the codebase')
    expect(wrapper.text()).toContain('explore')
    expect(wrapper.text()).toContain('completed')
  })

  it('emits openSubtask with the linked child session id', async () => {
    const wrapper = mount(HarnessSubtaskCard, {
      props: { part: makeSubtaskPart(), childSessionId: 'session-child' },
    })

    // Open the collapsible, then click the child-session link.
    const trigger = wrapper.find('[data-slot="collapsible-trigger"]')
    await trigger.trigger('click')
    const link = wrapper.find('button.mt-2')
    expect(link.exists()).toBe(true)
    await link.trigger('click')

    expect(wrapper.emitted('openSubtask')).toEqual([['session-child']])
  })

  it('store keeps the parent/child link for navigation', () => {
    const store = useHarnessStore()
    store.sessions = [
      {
        id: 'session-parent',
        workspace_id: 'workspace-1',
        parent_id: null,
        title: 'parent',
        mode: 'build',
        agent_name: 'build',
        model: 'm',
        status: 'idle',
        cost: 0,
        tokens: {},
      },
      {
        id: 'session-child',
        workspace_id: 'workspace-1',
        parent_id: 'session-parent',
        title: 'child',
        mode: 'build',
        agent_name: 'explore',
        model: 'm',
        status: 'idle',
        cost: 0,
        tokens: {},
      },
    ]
    store.messagesFor('session-parent').push({
      id: 'msg-1',
      session_id: 'session-parent',
      role: 'assistant',
      content: '',
      parts: [makeSubtaskPart()],
    })

    store.handleSubtaskFinished('session-parent', {
      subtask_id: 'sub-1',
      status: 'completed',
      summary: 'found it',
    })

    const parts = store.messagesFor('session-parent')[0]!.parts
    expect(parts[0]!.state).toBe('completed')
    // The child links back via parent_id — navigation target exists.
    const child = store.sessions.find((s) => s.parent_id === 'session-parent')
    expect(child?.id).toBe('session-child')
  })
})
