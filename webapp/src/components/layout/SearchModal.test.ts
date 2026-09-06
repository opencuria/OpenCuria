import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import SearchModal from './SearchModal.vue'

const routerPush = vi.fn()

const conversationStore = {
  conversations: [
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
  ],
  markAsRead: vi.fn(),
}

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

vi.mock('@/stores/harnessConversations', () => ({
  useHarnessConversationStore: () => conversationStore,
}))

function mountModal(props = { open: true }) {
  setActivePinia(createPinia())
  return mount(SearchModal, {
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

describe('SearchModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('lists conversations with workspace names', () => {
    const wrapper = mountModal()

    expect(wrapper.text()).toContain('First chat')
    expect(wrapper.text()).toContain('Alpha workspace')
  })

  it('filters by title and workspace', async () => {
    const wrapper = mountModal()
    const input = wrapper.find('[data-testid="chat-search-input"]')

    await input.setValue('beta')

    expect(wrapper.text()).toContain('Second chat')
    expect(wrapper.text()).not.toContain('First chat')
  })

  it('navigates on Enter like a sidebar row click', async () => {
    const wrapper = mountModal()
    const input = wrapper.find('[data-testid="chat-search-input"]')

    await input.trigger('keydown', { key: 'Enter' })

    expect(conversationStore.markAsRead).toHaveBeenCalledWith('s-1')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/workspaces/ws-1',
      query: { session: 's-1' },
    })
  })

  it('moves selection with ArrowDown/ArrowUp', async () => {
    const wrapper = mountModal()
    const input = wrapper.find('[data-testid="chat-search-input"]')

    await input.trigger('keydown', { key: 'ArrowDown' })

    const selected = wrapper.find('[aria-selected="true"]')
    expect(selected.text()).toContain('Second chat')
  })
})
