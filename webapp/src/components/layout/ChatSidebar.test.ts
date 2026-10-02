import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick } from 'vue'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ChatSidebar from './ChatSidebar.vue'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'

import { SidebarProvider } from '@/components/ui/sidebar'
import { WorkspaceStatus } from '@/types'
import type { HarnessConversation } from '@/types/harness'

enableAutoUnmount(afterEach)

const authStore = {
  organizations: [{ id: 'org-1', name: 'Acme', role: 'admin' }],
  activeOrganization: { id: 'org-1', name: 'Acme', role: 'admin' },
  activeOrganizationId: 'org-1',
  isAdmin: true,
  user: { email: 'admin@example.com' },
  setActiveOrganization: vi.fn(),
  logout: vi.fn(),
}

const workspaceStore = {
  workspaces: [
    {
      id: 'ws-1',
      name: 'Alpha',
      status: WorkspaceStatus.RUNNING,
      runner_online: true,
      last_activity_at: new Date().toISOString(),
      has_active_session: false,
      active_operation: null,
    },
    {
      id: 'ws-2',
      name: 'Beta',
      status: WorkspaceStatus.STOPPED,
      runner_online: false,
      last_activity_at: new Date().toISOString(),
      has_active_session: false,
      active_operation: null,
    },
  ],
  fetchWorkspaces: vi.fn(),
  updateWorkspaceStatus: vi.fn(),
  updateWorkspaceOperation: vi.fn(),
  updateWorkspaceRunnerOnline: vi.fn(),
  handleWorkspaceError: vi.fn(),
}

const defaultConversation: HarnessConversation = {
  session_id: 's-1',
  workspace_id: 'ws-1',
  workspace_name: 'Alpha',
  title: 'First chat',
  status: 'idle',
  mode: 'build',
  agent_name: 'build',
  model: '',
  unread: true,
  updated_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
  last_message_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
}

const conversationStore = {
  conversations: [defaultConversation] as HarnessConversation[],
  uniqueWorkspaceIds: ['ws-1'],
  fetchConversations: vi.fn(),
  markAsRead: vi.fn(),
  markAsUnread: vi.fn(),
  updateSessionStatus: vi.fn(),
}

const harnessStore = {
  viewingSessionId: null,
  renameSession: vi.fn(),
  removeSession: vi.fn(),
  handleSessionStatus: vi.fn(),
}

const routerPush = vi.fn()

vi.mock('vue-router', () => ({
  RouterLink: {
    name: 'RouterLink',
    template: '<a><slot /></a>',
  },
  useRoute: () => ({
    path: '/',
    name: 'home',
    params: {},
    query: {},
    meta: {},
  }),
  useRouter: () => ({
    go: vi.fn(),
    push: routerPush,
  }),
}))

vi.mock('@/composables/useTheme', () => ({
  useTheme: () => ({
    mode: { value: 'light' },
    setTheme: vi.fn(),
  }),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authStore,
}))

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/harnessConversations', () => ({
  useHarnessConversationStore: () => conversationStore,
}))

vi.mock('@/stores/harness', () => ({
  useHarnessStore: () => harnessStore,
}))

vi.mock('@/services/socket', () => ({
  connect: vi.fn(),
  disconnect: vi.fn(),
  isConnected: { value: true },
  subscribeToWorkspace: vi.fn(),
  unsubscribeFromWorkspace: vi.fn(),
  onEvent: () => () => {},
  onReconnect: () => () => {},
}))

const SidebarTestWrapper = defineComponent({
  components: { SidebarProvider, ChatSidebar },
  template: '<SidebarProvider><ChatSidebar /></SidebarProvider>',
})

const sidebarStubs = {
  OpenCuriaLogo: true,
  CommandPalette: true,
  Tooltip: { template: '<div><slot /></div>' },
  TooltipContent: true,
  TooltipTrigger: { template: '<div><slot /></div>' },
  TooltipProvider: { template: '<div><slot /></div>' },
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
  DropdownMenuSeparator: true,
}

function mountSidebar() {
  setActivePinia(createPinia())
  return mount(SidebarTestWrapper, {
    global: { stubs: sidebarStubs },
  })
}

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    ...defaultConversation,
    unread: false,
    ...overrides,
  }
}

describe('ChatSidebar', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    conversationStore.conversations = [makeConversation({ unread: true })]
    conversationStore.uniqueWorkspaceIds = ['ws-1']
    workspaceStore.workspaces = [
      {
        id: 'ws-1',
        name: 'Alpha',
        status: WorkspaceStatus.RUNNING,
        runner_online: true,
        last_activity_at: new Date().toISOString(),
        has_active_session: false,
        active_operation: null,
      },
      {
        id: 'ws-2',
        name: 'Beta',
        status: WorkspaceStatus.STOPPED,
        runner_online: false,
        last_activity_at: new Date().toISOString(),
        has_active_session: false,
        active_operation: null,
      },
    ]
  })

  it('renders the OpenCuria brand name', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('OpenCuria')
    expect(wrapper.text()).not.toContain('Acme')
  })

  it('starts each shared data poll with a single initial request', () => {
    const wrapper = mountSidebar()

    expect(workspaceStore.fetchWorkspaces).toHaveBeenCalledTimes(1)
    expect(conversationStore.fetchConversations).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('shows unread chats in their workspace and hides empty stopped workspaces', () => {
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-testid="active-section"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="time-list"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="workspace-section"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('First chat')
    expect(wrapper.text()).toContain('Alpha')
    expect(wrapper.text()).toContain('All workspaces (2)')
    expect(wrapper.text()).not.toContain('Beta')
    expect(wrapper.text()).not.toContain('Keine Chats — Enter zum Starten')
    expect(wrapper.findAll('[data-testid="unread-dot"]')).toHaveLength(1)
  })

  it('shows action-required chats at the top and in their workspace', () => {
    conversationStore.conversations = [
      makeConversation({
        session_id: 's-gate',
        title: 'Needs a permission',
        status: 'busy',
        needs_attention: true,
        attention_kind: 'permission',
      }),
    ]
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-testid="action-required-section"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Action required')
    expect(wrapper.text()).toContain('Needs a permission')
    expect(wrapper.findAll('[data-testid="attention-icon"]')).toHaveLength(2)
    expect(wrapper.get('[data-testid="workspace-conversation-group"]').text()).toContain(
      'Needs a permission',
    )
    expect(wrapper.find('[data-testid="active-section"]').exists()).toBe(false)
  })

  it('renders new chat and search actions', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('New chat')
    expect(wrapper.text()).toContain('Search')
  })

  it('navigates to the workspace thread on chat click', async () => {
    const wrapper = mountSidebar()

    const row = wrapper.find('[aria-label="Open chat First chat"]')
    await row.trigger('click')

    expect(conversationStore.markAsRead).toHaveBeenCalledWith('s-1')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/workspaces/ws-1',
      query: { session: 's-1' },
    })
  })

  it('starts with four chats and reveals four more per click', async () => {
    conversationStore.conversations = Array.from({ length: 20 }, (_, index) =>
      makeConversation({
        session_id: `s-${index}`,
        title: `Chat ${index}`,
        unread: false,
        last_message_at: new Date(Date.now() - index * 60 * 1000).toISOString(),
      }),
    )

    const wrapper = mountSidebar()

    expect(wrapper.find('[data-testid="active-section"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="conversation-row"]')).toHaveLength(4)
    expect(wrapper.get('[data-testid="show-more-chats"]').text()).toContain('Show 4 more chats')

    await wrapper.get('[data-testid="show-more-chats"]').trigger('click')

    expect(wrapper.findAll('[data-testid="conversation-row"]')).toHaveLength(8)
  })

  it('marks unread chats in all workspaces as read from the shared header', async () => {
    conversationStore.conversations = [
      makeConversation({ session_id: 'unread-1', unread: true }),
      makeConversation({ session_id: 'unread-2', workspace_id: 'ws-2', unread: true }),
      makeConversation({ session_id: 'read', unread: false }),
    ]
    const wrapper = mountSidebar()
    await wrapper.get('[data-testid="mark-all-read"]').trigger('click')
    expect(conversationStore.markAsRead.mock.calls).toEqual([['unread-1'], ['unread-2']])
  })

  it('opens workspace navigation and management from the merged list', async () => {
    const wrapper = mountSidebar()
    await wrapper.get('[aria-label="Open workspace Alpha"]').trigger('click')
    expect(routerPush).toHaveBeenLastCalledWith({ path: '/workspaces/ws-1' })
    await wrapper.get('[data-testid="all-workspaces"]').trigger('click')
    expect(routerPush).toHaveBeenLastCalledWith('/workspaces')
    await wrapper.get('[data-testid="workspaces-create"]').trigger('click')
    expect(routerPush).toHaveBeenLastCalledWith('/workspaces')
  })

  it('forwards rename and read actions from workspace chat rows', async () => {
    const wrapper = mountSidebar()
    await wrapper.get('[data-testid="mark-read-item"]').trigger('click')
    expect(conversationStore.markAsRead).toHaveBeenCalledWith('s-1')
    const rename = wrapper.findAll('button').find((button) => button.text() === 'Rename')!
    await rename.trigger('click')
    await wrapper.get('[data-testid="conversation-rename-input"]').setValue('Renamed chat')
    await wrapper.get('[data-testid="conversation-rename-input"]').trigger('keydown.enter')
    expect(harnessStore.renameSession).toHaveBeenCalledWith('s-1', 'Renamed chat')
  })

  it('places scheduled tasks after the workspace chat section and opens settings without navigation', async () => {
    conversationStore.conversations = [makeConversation({ session_id: 'history', unread: false })]
    const wrapper = mountSidebar()
    const store = useScheduledTaskStore()
    store.tasks = [
      {
        id: 'task-1',
        name: 'Morning check',
        workspace_id: 'ws-1',
        prompt: 'Check',
        mode: 'build',
        model: '',
        reasoning_effort: '',
        skill_ids: [],
        recurrence: 'daily',
        weekdays: [],
        local_time: '09:00',
        timezone_name: 'UTC',
        enabled: false,
        next_run_at: '2026-10-08T09:00:00Z',
        created_at: '',
        updated_at: '',
      },
    ]
    await nextTick()
    expect(wrapper.find('[data-testid="scheduled-tasks-section"]').exists()).toBe(true)
    expect(
      wrapper
        .find('[data-testid="workspace-conversation-list"]')
        .element.compareDocumentPosition(
          wrapper.find('[data-testid="scheduled-tasks-section"]').element,
        ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    const before = routerPush.mock.calls.length
    await wrapper.get('[data-testid="scheduled-task-task-1"]').trigger('click')
    expect(store.dialogOpen).toBe(true)
    expect(store.selectedTaskId).toBe('task-1')
    expect(routerPush).toHaveBeenCalledTimes(before)
  })

  it('shows scheduled task titles on their own line with concise cadence and pauses', async () => {
    conversationStore.conversations = []
    const wrapper = mountSidebar()
    const store = useScheduledTaskStore()
    store.tasks = [
      {
        id: 'long-task',
        name: 'A full task title remains visible',
        workspace_id: 'ws-1',
        prompt: 'Check',
        mode: 'build',
        model: '',
        reasoning_effort: '',
        skill_ids: [],
        recurrence: 'weekly',
        weekdays: [0, 2],
        local_time: '09:00',
        timezone_name: 'UTC',
        enabled: false,
        next_run_at: '2026-10-08T09:00:00Z',
        created_at: '',
        updated_at: '',
      },
    ]
    await nextTick()
    const row = wrapper.get('[data-testid="scheduled-task-long-task"]')
    expect(row.attributes('aria-label')).toContain('A full task title remains visible')
    expect(row.text()).toContain('Mon, Wed · 09:00')
    expect(row.find('.lucide-circle-pause').exists()).toBe(true)
    expect(row.classes()).toContain('min-h-11')
  })

  it('shows an empty chat prompt when there are no conversations', () => {
    conversationStore.conversations = []
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('No chats yet — start with New chat')
  })

  it('emits opencuria:open-settings from the user menu', async () => {
    const wrapper = mountSidebar()
    const events: Event[] = []
    const listener = (e: Event) => events.push(e)
    window.addEventListener('opencuria:open-settings', listener)

    try {
      const settingsItem = wrapper
        .findAll('button')
        .find((item) => item.text().includes('Open settings'))
      expect(settingsItem).toBeTruthy()
      await settingsItem!.trigger('click')
      expect(events.length).toBeGreaterThan(0)
    } finally {
      window.removeEventListener('opencuria:open-settings', listener)
    }
  })
})
