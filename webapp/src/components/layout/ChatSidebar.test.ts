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
  user: { id: 1, email: 'admin@example.com' },
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
    expect(wrapper.get('[data-testid="inbox-count"]').text()).toBe('1')
    expect(wrapper.findAll('[data-testid="inbox-result-icon"]')).toHaveLength(1)
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

    expect(wrapper.find('[data-testid="inbox-section"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="inbox-section"]').text()).toContain('Inbox')
    expect(wrapper.text()).toContain('Needs a permission')
    expect(wrapper.findAll('[data-testid="attention-icon"]')).toHaveLength(2)
    expect(wrapper.get('[data-testid="workspace-conversation-group"]').text()).toContain(
      'Needs a permission',
    )
    expect(wrapper.find('[data-testid="active-section"]').exists()).toBe(false)
  })

  it('hides stopped-workspace chats even when they require action', () => {
    conversationStore.conversations = [
      makeConversation({ workspace_id: 'ws-2', workspace_name: 'Beta', needs_attention: true }),
    ]
    const wrapper = mountSidebar()
    expect(wrapper.find('[data-testid="inbox-section"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Open workspace Beta"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('First chat')
  })

  it('combines gates and idle unread across workspaces with one count and no busy results', () => {
    conversationStore.conversations = [
      makeConversation({ session_id: 'build', unread: true }),
      makeConversation({
        session_id: 'stopped-plan',
        title: 'Archived plan',
        workspace_id: 'ws-2',
        workspace_name: 'Beta',
        unread: true,
        mode: 'plan',
        agent_name: 'plan',
      }),
      makeConversation({
        session_id: 'question',
        status: 'busy',
        unread: true,
        needs_attention: true,
        attention_kind: 'question',
      }),
      makeConversation({
        session_id: 'busy',
        title: 'Still running',
        status: 'busy',
        unread: true,
        manual_unread: true,
      }),
      makeConversation({ session_id: 'read', title: 'Read chat' }),
    ]
    const wrapper = mountSidebar()
    const inbox = wrapper.get('[data-testid="inbox-section"]')
    expect(wrapper.get('[data-testid="inbox-count"]').text()).toBe('3')
    expect(inbox.findAll('[data-testid="conversation-row"]')).toHaveLength(3)
    expect(inbox.findAll('[data-testid="attention-icon"]')).toHaveLength(1)
    expect(
      inbox
        .findAll('[data-testid="inbox-result-icon"]')
        .map((icon) => icon.attributes('data-kind')),
    ).toEqual(['build', 'plan'])
    expect(inbox.text()).toContain('Archived plan')
    expect(inbox.text()).not.toContain('Still running')
    expect(inbox.text()).not.toContain('Read chat')
    expect(wrapper.find('[aria-label="Open workspace Beta"]').exists()).toBe(false)
    expect(
      wrapper
        .get('[data-testid="workspace-conversation-list"]')
        .find('[data-testid="inbox-result-icon"]')
        .exists(),
    ).toBe(false)
  })

  it('hides the inbox when there are only read or busy chats', () => {
    conversationStore.conversations = [
      makeConversation(),
      makeConversation({ session_id: 'busy', status: 'busy', unread: true }),
    ]
    expect(mountSidebar().find('[data-testid="inbox-section"]').exists()).toBe(false)
  })

  it('opens and marks a stopped workspace inbox chat as read', async () => {
    conversationStore.conversations = [
      makeConversation({
        session_id: 'stopped-plan',
        workspace_id: 'ws-2',
        workspace_name: 'Beta',
        unread: true,
        mode: 'plan',
        agent_name: 'plan',
      }),
    ]
    const wrapper = mountSidebar()
    await wrapper
      .get('[data-testid="inbox-section"]')
      .get('[data-testid="conversation-row"]')
      .trigger('click')
    expect(conversationStore.markAsRead).toHaveBeenCalledWith('stopped-plan')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/workspaces/ws-2',
      query: { session: 'stopped-plan' },
    })
  })

  it('persists collapsed workspace IDs across sidebar remounts, without navigating', async () => {
    const wrapper = mountSidebar()
    await wrapper.get('[aria-label="Collapse workspace Alpha"]').trigger('click')
    expect(
      wrapper
        .get('[data-testid="workspace-conversation-group"]')
        .find('[data-testid="conversation-row"]')
        .exists(),
    ).toBe(false)
    expect(
      wrapper
        .get('[data-testid="inbox-section"]')
        .find('[data-testid="conversation-row"]')
        .exists(),
    ).toBe(true)
    expect(routerPush).not.toHaveBeenCalled()
    expect(
      JSON.parse(localStorage.getItem('opencuria-sidebar-collapsed-workspaces:1:org-1')!),
    ).toEqual(['ws-1'])
    wrapper.unmount()
    const reopened = mountSidebar()
    expect(reopened.get('[aria-label="Expand workspace Alpha"]').attributes('aria-expanded')).toBe(
      'false',
    )
    expect(
      reopened
        .get('[data-testid="workspace-conversation-group"]')
        .find('[data-testid="conversation-row"]')
        .exists(),
    ).toBe(false)
    await reopened.get('[aria-label="Expand workspace Alpha"]').trigger('click')
    expect(reopened.find('[data-testid="conversation-row"]').exists()).toBe(true)
    expect(
      JSON.parse(localStorage.getItem('opencuria-sidebar-collapsed-workspaces:1:org-1')!),
    ).toEqual([])
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

  it('marks only unread chats from running workspaces as read', async () => {
    conversationStore.conversations = [
      makeConversation({ session_id: 'unread-1', unread: true }),
      makeConversation({ session_id: 'unread-2', workspace_id: 'ws-2', unread: true }),
      makeConversation({ session_id: 'read', unread: false }),
    ]
    const wrapper = mountSidebar()
    await wrapper.get('[data-testid="mark-all-read"]').trigger('click')
    expect(conversationStore.markAsRead.mock.calls).toEqual([['unread-1']])
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
    await wrapper
      .get('[data-testid="workspace-conversation-group"]')
      .get('[data-testid="mark-read-item"]')
      .trigger('click')
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

  it('shows empty chat state for a running workspace', () => {
    conversationStore.conversations = []
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('No chats yet')
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
