import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import CommandPalette from './CommandPalette.vue'
import { WorkspaceStatus } from '@/types'

const routerPush = vi.fn()

const baseConversations = [
    {
      session_id: 's-1',
      workspace_id: 'ws-1',
      workspace_name: 'Alpha workspace',
      title: 'First chat',
      status: 'idle',
      mode: 'build',
      agent_name: 'build',
      model: '',
      unread: false,
      updated_at: new Date().toISOString(),
    },
    {
      session_id: 's-2',
      workspace_id: 'ws-2',
      workspace_name: 'Beta workspace',
      title: 'Second chat',
      status: 'idle',
      mode: 'plan',
      agent_name: 'plan',
      model: '',
      unread: false,
      updated_at: new Date().toISOString(),
    },
  ]

const conversationStore = {
  conversations: [...baseConversations],
  markAsRead: vi.fn(),
}

const workspaceStore = {
  workspaces: [
    {
      id: 'ws-1',
      name: 'Alpha workspace',
      status: WorkspaceStatus.RUNNING,
      runner_online: true,
      last_activity_at: new Date().toISOString(),
      has_active_session: false,
      active_operation: null,
    },
    {
      id: 'ws-2',
      name: 'Beta workspace',
      status: WorkspaceStatus.RUNNING,
      runner_online: true,
      last_activity_at: new Date().toISOString(),
      has_active_session: false,
      active_operation: null,
    },
  ],
}

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

vi.mock('@/stores/harnessConversations', () => ({
  useHarnessConversationStore: () => conversationStore,
}))

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

function mountPalette(props = { open: true }) {
  setActivePinia(createPinia())
  return mount(CommandPalette, {
    props,
    global: {
      stubs: {
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: { template: '<div><slot /></div>' },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('CommandPalette', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    conversationStore.conversations = [...baseConversations]
  })

  it('lists actions, recent chats, and workspaces without a query', () => {
    const wrapper = mountPalette()

    expect(wrapper.text()).toContain('Neuer Chat')
    expect(wrapper.text()).toContain('First chat')
    expect(wrapper.text()).toContain('Alpha workspace')
    expect(wrapper.text()).toContain('Workspaces verwalten')
  })

  it('filters chats, workspaces, and actions by query', async () => {
    const wrapper = mountPalette()
    const input = wrapper.find('[data-testid="command-palette-input"]')

    await input.setValue('beta')

    expect(wrapper.text()).toContain('Second chat')
    expect(wrapper.text()).toContain('Beta workspace')
    expect(wrapper.text()).not.toContain('First chat')
    expect(wrapper.text()).not.toContain('Neuer Chat')
  })

  it('runs Neuer Chat on Enter when the query is empty', async () => {
    const wrapper = mountPalette()
    const input = wrapper.find('[data-testid="command-palette-input"]')

    await input.trigger('keydown', { key: 'Enter' })

    expect(routerPush).toHaveBeenCalledWith('/')
  })

  it('opens a matching chat on Enter', async () => {
    const wrapper = mountPalette()
    const input = wrapper.find('[data-testid="command-palette-input"]')

    await input.setValue('first')
    await input.trigger('keydown', { key: 'Enter' })

    expect(conversationStore.markAsRead).toHaveBeenCalledWith('s-1')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/workspaces/ws-1',
      query: { session: 's-1' },
    })
  })

  it('moves selection with ArrowDown/ArrowUp', async () => {
    const wrapper = mountPalette()
    const input = wrapper.find('[data-testid="command-palette-input"]')

    await input.trigger('keydown', { key: 'ArrowDown' })

    const selected = wrapper.find('[aria-selected="true"]')
    expect(selected.text()).toContain('Workspaces verwalten')
  })

  it('lists action-required chats above recent chats', () => {
    conversationStore.conversations = [
      ...baseConversations,
      {
        session_id: 's-gate',
        workspace_id: 'ws-1',
        workspace_name: 'Alpha workspace',
        title: 'Waiting chat',
        status: 'busy',
        mode: 'build',
        agent_name: 'build',
        model: '',
        unread: false,
        needs_attention: true,
        attention_kind: 'question',
        updated_at: new Date().toISOString(),
      },
    ]
    const wrapper = mountPalette()

    expect(wrapper.text()).toContain('Action required')
    expect(wrapper.text()).toContain('Waiting chat')
    expect(wrapper.find('[data-testid="palette-attention-badge"]').exists()).toBe(true)
  })
})
