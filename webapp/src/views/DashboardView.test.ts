import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import DashboardView from '@/views/DashboardView.vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { WorkspaceStatus, RuntimeType, type Workspace } from '@/types'

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
    getProviderConfig: vi.fn().mockResolvedValue({
      base_url: '',
      default_model: '',
      small_model: '',
      has_api_key: false,
      api_key_hint: '',
    }),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
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

vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({
    credentials: [],
    fetchCredentials: vi.fn().mockResolvedValue(undefined),
  }),
}))

const createHarnessSessionMock = vi.fn()

vi.mock('@/stores/harness', () => ({
  useHarnessStore: () => ({
    sessions: [],
    activeSessionId: null,
    activeSession: null,
    activeMessages: [],
    activeTodos: [],
    activePermissionRequests: [],
    loading: false,
    modelInput: '',
    setActiveSession: vi.fn(),
    fetchSessions: vi.fn().mockResolvedValue(undefined),
    createSession: createHarnessSessionMock,
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
    ...overrides,
  }
}

function makeRouter() {
  const router = createRouter({
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
  return router
}

describe('DashboardView workspace navigation + new harness chat', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace()]
  })

  it('routes to workspace-detail when a workspace card is clicked', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()

    const wrapper = mount(DashboardView, {
      global: { plugins: [router] },
    })
    await wrapper.vm.$nextTick()

    const card = wrapper.findComponent({ name: 'WorkspaceCard' })
    expect(card.exists()).toBe(true)
    await card.vm.$emit('click')
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(router.currentRoute.value.name).toBe('workspace-detail')
    expect(router.currentRoute.value.params.id).toBe('ws-1')
  })

  it('creates a harness session from the dashboard and navigates to the workspace', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const pushSpy = vi.spyOn(router, 'push')

    createHarnessSessionMock.mockResolvedValue({ id: 'session-1', workspace_id: 'ws-1' })

    const wrapper = mount(DashboardView, {
      global: { plugins: [router] },
    })
    await wrapper.vm.$nextTick()

    const newChatButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('New Chat'))
    expect(newChatButton).toBeTruthy()
    await newChatButton!.trigger('click')

    // The new-chat dialog hosts the session switcher composer.
    const switcher = wrapper.findComponent({ name: 'HarnessSessionSwitcher' })
    expect(switcher.exists()).toBe(true)
    await (switcher.vm as unknown as { $emit: (...args: unknown[]) => void }).$emit(
      'create',
      'hello agent',
      'build',
      '',
    )
    await wrapper.vm.$nextTick()

    expect(createHarnessSessionMock).toHaveBeenCalledWith('ws-1', 'hello agent', 'build', '')
    expect(pushSpy).toHaveBeenCalledWith({
      name: 'workspace-detail',
      params: { id: 'ws-1' },
    })
  })
})
