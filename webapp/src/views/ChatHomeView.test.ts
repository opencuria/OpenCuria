import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ChatHomeView from './ChatHomeView.vue'
import { WorkspaceStatus, RuntimeType, type Workspace } from '@/types'

const workspaceStore = {
  workspaces: [] as Workspace[],
  fetchWorkspaces: vi.fn().mockResolvedValue(undefined),
  getWorkspaceTransitionLabel: vi.fn().mockReturnValue(null),
}

const harnessStore = {
  modelInput: '',
  effortInput: '',
  activeSessionId: null as string | null,
  createSession: vi.fn(),
}

const skillStore = {
  skills: [] as never[],
  fetchSkills: vi.fn().mockResolvedValue(undefined),
}

const authStore = {
  user: { email: 'Ada@example.com' },
}

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/harness', () => ({
  useHarnessStore: () => harnessStore,
}))

vi.mock('@/stores/skills', () => ({
  useSkillStore: () => skillStore,
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authStore,
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
      computer_use_model: '',
      has_api_key: true,
      api_key_hint: '',
    }),
  }
})

vi.mock('@/lib/providerCatalog', () => ({
  loadProviderModelsCached: vi.fn().mockResolvedValue([]),
}))

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'ws-1',
    runner_id: 'runner-1',
    status: WorkspaceStatus.RUNNING,
    active_operation: null,
    name: 'Alpha',
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

function makeRouter() {
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/', name: 'home', component: { template: '<div />' } },
      {
        path: '/workspaces/:id',
        name: 'workspace-detail',
        component: { template: '<div />' },
      },
    ],
  })
}

async function mountHome() {
  const router = makeRouter()
  await router.push('/')
  await router.isReady()
  const wrapper = mount(ChatHomeView, {
    global: {
      plugins: [router],
      stubs: {
        CreateWorkspaceDialog: { template: '<div />' },
        HarnessChatInput: {
          template:
            '<div data-testid="chat-home-composer"><textarea data-testid="composer-textarea" /></div>',
        },
        DropdownMenu: { template: '<div><slot /></div>' },
        DropdownMenuTrigger: { template: '<div><slot /></div>' },
        DropdownMenuContent: { template: '<div><slot /></div>' },
        DropdownMenuItem: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
        DropdownMenuSeparator: true,
      },
    },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('ChatHomeView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    workspaceStore.workspaces = [makeWorkspace()]
    harnessStore.activeSessionId = null
    harnessStore.createSession.mockReset()
  })

  it('greets with the email prefix and renders picker, composer and suggestions', async () => {
    const { wrapper } = await mountHome()

    expect(wrapper.get('[data-testid="chat-home-greeting"]').text()).toContain('Ada')
    expect(wrapper.find('[data-testid="workspace-picker-trigger"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-textarea"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="chat-home-suggestion"]')).toHaveLength(4)
  })

  it('prefers the last selected workspace from localStorage', async () => {
    workspaceStore.workspaces = [
      makeWorkspace(),
      makeWorkspace({ id: 'ws-2', name: 'Beta' }),
    ]
    localStorage.setItem('opencuria:last-workspace', 'ws-2')

    const { wrapper } = await mountHome()

    expect(wrapper.get('[data-testid="workspace-picker-trigger"]').text()).toContain('Beta')
  })

  it('creates a session and navigates to workspace-detail on send', async () => {
    const { wrapper, router } = await mountHome()
    const pushSpy = vi.spyOn(router, 'push')
    harnessStore.createSession.mockResolvedValue({ id: 'session-1' })

    const vm = wrapper.vm as unknown as {
      handleSend: (
        prompt: string,
        mode: 'build',
        model: string,
        skillIds: string[],
        effort: string,
      ) => Promise<void>
    }
    await vm.handleSend('hello', 'build', '', [], '')
    await flushPromises()

    expect(harnessStore.createSession).toHaveBeenCalledWith('ws-1', 'hello', 'build', '', [], '')
    expect(pushSpy).toHaveBeenCalledWith({
      name: 'workspace-detail',
      params: { id: 'ws-1' },
      query: { session: 'session-1' },
    })
  })

  it('shows an empty state when no workspaces exist', async () => {
    workspaceStore.workspaces = []
    const { wrapper } = await mountHome()

    expect(wrapper.find('[data-testid="chat-home-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-textarea"]').exists()).toBe(false)
  })
})
