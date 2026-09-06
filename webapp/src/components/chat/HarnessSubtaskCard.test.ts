import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessSubtaskCard from './HarnessSubtaskCard.vue'
import { useHarnessStore } from '@/stores/harness'
import type { HarnessPart } from '@/types/harness'

vi.mock('./HarnessDesktopMini.vue', () => ({
  default: { template: '<div data-testid="harness-desktop-mini" />' },
}))

function makeSubtaskPart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-sub-1',
    message_id: 'msg-1',
    session_id: 'session-parent',
    type: 'subtask',
    state: 'completed',
    title: 'Find desktop VNC renderer',
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

  it('renders a completed timeline row with type and Completed', () => {
    const wrapper = mount(HarnessSubtaskCard, {
      props: { part: makeSubtaskPart() },
    })

    expect(wrapper.get('[data-testid="harness-subtask-row"]').text()).toContain(
      'Find desktop VNC renderer',
    )
    expect(wrapper.get('[data-testid="harness-subtask-type"]').text()).toBe('Explorer')
    expect(wrapper.get('[data-testid="harness-subtask-activity"]').text()).toBe(
      'Completed',
    )
    expect(wrapper.get('[data-testid="harness-subtask-indicator"]').attributes('data-running')).toBe(
      '0',
    )
    expect(wrapper.text()).not.toContain('explore')
  })

  it('shows a running indicator and no activity without a child tool', () => {
    const wrapper = mount(HarnessSubtaskCard, {
      props: { part: makeSubtaskPart({ state: 'running', output: '' }) },
    })

    expect(wrapper.get('[data-testid="harness-subtask-indicator"]').attributes('data-running')).toBe(
      '1',
    )
    expect(wrapper.find('[data-testid="harness-subtask-activity"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Completed')
  })

  it('shows the running child tool title as activity', () => {
    const store = useHarnessStore()
    store.messagesBySession['session-child'] = [
      {
        id: 'child-msg',
        session_id: 'session-child',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'child-tool',
            session_id: 'session-child',
            type: 'tool',
            state: 'running',
            tool: 'read',
            title: 'Read renderer.ts',
            output: '',
          },
        ],
      },
    ]

    const wrapper = mount(HarnessSubtaskCard, {
      props: {
        part: makeSubtaskPart({ state: 'running', output: '' }),
        childSessionId: 'session-child',
      },
    })

    expect(wrapper.get('[data-testid="harness-subtask-activity"]').text()).toBe(
      'Read renderer.ts',
    )
  })

  it('opens the only child session even when titles differ', async () => {
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
        title: 'Generated login test title',
        mode: 'build',
        agent_name: 'computeruse',
        model: 'm',
        status: 'idle',
        cost: 0,
        tokens: {},
      },
    ]

    const wrapper = mount(HarnessSubtaskCard, {
      props: {
        part: makeSubtaskPart({
          title: 'Webapp Login und Dashboard testen',
          meta: { subtask_id: 'sub-1', agent: 'computeruse' },
        }),
      },
    })

    expect(wrapper.get('[data-testid="harness-subtask-row"]').classes()).toContain(
      'cursor-pointer',
    )
    await wrapper.get('[data-testid="harness-subtask-row"]').trigger('click')
    expect(wrapper.emitted('openSubtask')).toEqual([['session-child']])
  })

  it('highlights the row on hover when it is clickable', () => {
    const wrapper = mount(HarnessSubtaskCard, {
      props: { part: makeSubtaskPart(), childSessionId: 'session-child' },
    })

    const classes = wrapper.get('[data-testid="harness-subtask-row"]').classes()
    expect(classes).toContain('hover:bg-muted/60')
    expect(classes).toContain('cursor-pointer')
  })

  it('shows a live desktop mini only for a running computer-use subtask', () => {
    const running = mount(HarnessSubtaskCard, {
      props: {
        part: makeSubtaskPart({
          state: 'running',
          output: '',
          meta: { subtask_id: 'sub-1', agent: 'computeruse', child_session_id: 'cu-1' },
        }),
        childSessionId: 'cu-1',
      },
    })
    expect(running.get('[data-testid="harness-subtask-type"]').text()).toBe('Computer use')
    expect(running.find('[data-testid="harness-desktop-mini"]').exists()).toBe(true)

    const done = mount(HarnessSubtaskCard, {
      props: {
        part: makeSubtaskPart({
          meta: { subtask_id: 'sub-1', agent: 'computeruse', child_session_id: 'cu-1' },
        }),
        childSessionId: 'cu-1',
      },
    })
    expect(done.find('[data-testid="harness-desktop-mini"]').exists()).toBe(false)
  })

  it('shows Failed when the subtask errored', () => {
    const wrapper = mount(HarnessSubtaskCard, {
      props: { part: makeSubtaskPart({ state: 'error', output: 'boom' }) },
    })

    expect(wrapper.get('[data-testid="harness-subtask-activity"]').text()).toBe('Failed')
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
    const child = store.sessions.find((s) => s.parent_id === 'session-parent')
    expect(child?.id).toBe('session-child')
  })
})
