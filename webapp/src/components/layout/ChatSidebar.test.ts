import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ChatSidebar from './ChatSidebar.vue'
import { SidebarProvider } from '@/components/ui/sidebar'
import { WorkspaceStatus } from '@/types'

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
    },
    {
      id: 'ws-2',
      name: 'Beta',
      status: WorkspaceStatus.STOPPED,
      runner_online: false,
    },
  ],
  fetchWorkspaces: vi.fn(),
  updateWorkspaceStatus: vi.fn(),
  updateWorkspaceOperation: vi.fn(),
  updateWorkspaceRunnerOnline: vi.fn(),
  handleWorkspaceError: vi.fn(),
}

const conversationStore = {
  conversations: [
    {
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
    },
  ],
  uniqueWorkspaceIds: ['ws-1'],
  fetchConversations: vi.fn(),
  markAsRead: vi.fn(),
  updateSessionStatus: vi.fn(),
  touchConversation: vi.fn(),
}

const harnessStore = {
  childSessionsByParent: {},
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
}))

const SidebarTestWrapper = defineComponent({
  components: { SidebarProvider, ChatSidebar },
  template: '<SidebarProvider><ChatSidebar /></SidebarProvider>',
})

function mountSidebar() {
  setActivePinia(createPinia())
  return mount(SidebarTestWrapper, {
    global: {
      stubs: {
        OpenCuriaLogo: true,
        SearchModal: true,
        Tooltip: { template: '<div><slot /></div>' },
        TooltipContent: true,
        TooltipTrigger: { template: '<div><slot /></div>' },
        TooltipProvider: { template: '<div><slot /></div>' },
        // Render dropdown/collapsible content inline (reka teleports it otherwise)
        DropdownMenu: { template: '<div><slot /></div>' },
        DropdownMenuTrigger: { template: '<div><slot /></div>' },
        DropdownMenuContent: { template: '<div><slot /></div>' },
        DropdownMenuItem: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
        DropdownMenuSeparator: true,
        Collapsible: { template: '<div><slot /></div>' },
        CollapsibleTrigger: { template: '<button><slot /></button>' },
        CollapsibleContent: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('ChatSidebar', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('renders the active organization name', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('Acme')
  })

  it('renders workspace groups with chats and empty state', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('Alpha')
    expect(wrapper.text()).toContain('First chat')
    expect(wrapper.text()).toContain('Keine Chats — Enter zum Starten')
  })

  it('shows an unread dot for unread conversations', () => {
    const wrapper = mountSidebar()

    expect(wrapper.findAll('[data-testid="unread-dot"]')).toHaveLength(1)
  })

  it('renders new chat and search actions', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('Neuer Chat')
    expect(wrapper.text()).toContain('Suchen')
  })

  it('navigates to the workspace thread on chat click', async () => {
    const wrapper = mountSidebar()

    const row = wrapper.find('[aria-label="Chat First chat öffnen"]')
    await row.trigger('click')

    expect(conversationStore.markAsRead).toHaveBeenCalledWith('s-1')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/workspaces/ws-1',
      query: { session: 's-1' },
    })
  })

  it('emits opencuria:open-settings from the user menu', async () => {
    const wrapper = mountSidebar()
    const events: Event[] = []
    const listener = (e: Event) => events.push(e)
    window.addEventListener('opencuria:open-settings', listener)

    try {
      const settingsItem = wrapper
        .findAll('button')
        .find((item) => item.text().includes('Einstellungen öffnen'))
      expect(settingsItem).toBeTruthy()
      await settingsItem!.trigger('click')
      expect(events.length).toBeGreaterThan(0)
    } finally {
      window.removeEventListener('opencuria:open-settings', listener)
    }
  })
})
