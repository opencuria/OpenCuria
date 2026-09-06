import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import DashboardView from '@/views/DashboardView.vue'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { useWorkspaceStore } from '@/stores/workspaces'
import { WorkspaceStatus, RuntimeType, type Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'

vi.mock('@/services/socket', () => ({
  subscribeToWorkspace: vi.fn(),
  unsubscribeFromWorkspace: vi.fn(),
  onEvent: vi.fn(() => () => {}),
}))

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessConversations: vi.fn().mockResolvedValue([]),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
    getProviderConfig: vi.fn().mockResolvedValue({
      base_url: '',
      default_model: '',
      small_model: '',
      computer_use_model: '',
      has_api_key: false,
      api_key_hint: '',
    }),
    listProviderModels: vi.fn().mockResolvedValue([]),
  }
})

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => ({
    runners: [],
    onlineRunners: [],
    fetchRunners: vi.fn().mockResolvedValue(undefined),
    runnerById: vi.fn().mockReturnValue(undefined),
  }),
}))

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'ws-1',
    runner_id: 'runner-1',
    status: WorkspaceStatus.RUNNING,
    active_operation: null,
    name: 'Workspace One',
    runtime_type: RuntimeType.DOCKER,
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    last_activity_at: '2026-03-29T10:00:00.000Z',
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    created_at: '2026-03-29T10:00:00.000Z',
    updated_at: '2026-03-29T10:00:00.000Z',
    has_active_session: false,
    runner_online: true,
    credential_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 'session-1',
    workspace_id: 'ws-1',
    workspace_name: 'Workspace One',
    title: 'Dashboard chat',
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    model: '',
    unread: false,
    updated_at: '2026-03-29T10:00:00.000Z',
    ...overrides,
  }
}

function makeRouter() {
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/', name: 'dashboard', component: { template: '<div />' } },
      {
        path: '/workspaces/:id',
        name: 'workspace-detail',
        component: { template: '<div />' },
      },
    ],
  })
}

describe('DashboardView harness conversations', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.removeItem('opencuria:dashboard-view')
    const workspaceStore = useWorkspaceStore()
    workspaceStore.workspaces = [makeWorkspace()]
    const conversationStore = useHarnessConversationStore()
    conversationStore.conversations = [makeConversation()]
  })

  it('shows kanban/list toggle and search conversations input', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()

    const wrapper = mount(DashboardView, {
      global: { plugins: [router] },
    })
    await flushPromises()

    expect(wrapper.find('input[placeholder="Search conversations..."]').exists()).toBe(true)
    expect(wrapper.find('[title="List view"]').exists()).toBe(true)
    expect(wrapper.find('[title="Kanban view"]').exists()).toBe(true)
  })

  it('persists selected dashboard view mode in localStorage', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()

    const wrapper = mount(DashboardView, {
      global: { plugins: [router] },
    })
    await flushPromises()

    await wrapper.get('[title="List view"]').trigger('click')
    expect(localStorage.getItem('opencuria:dashboard-view')).toBe('list')
  })

  it('navigates to workspace detail with session query on card click', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const pushSpy = vi.spyOn(router, 'push')

    const wrapper = mount(DashboardView, {
      global: { plugins: [router] },
    })
    await flushPromises()

    const listView = wrapper.findComponent({ name: 'HarnessConversationListView' })
    await listView.vm.$emit('conversationClick', makeConversation())
    await flushPromises()

    expect(pushSpy).toHaveBeenCalledWith({
      path: '/workspaces/ws-1',
      query: { session: 'session-1' },
    })
  })
})
