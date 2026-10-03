import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EditWorkspaceDialog from './EditWorkspaceDialog.vue'
import { RuntimeType, WorkspaceStatus, type Workspace } from '@/types'
import { saveWorkspaceDraft } from '@/lib/workspaceDraft'

const mocks = vi.hoisted(() => ({
  routerPush: vi.fn(),
  fetchCredentials: vi.fn(),
  fetchRunners: vi.fn(),
  updateWorkspace: vi.fn(),
  fetchWorkspaceDetail: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  auth: { user: { id: 1 }, activeOrganizationId: 'org-1', initialized: true },
  runner: null as Record<string, unknown> | null,
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.routerPush }) }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({
    credentials: [],
    error: null,
    loading: false,
    fetchCredentials: mocks.fetchCredentials,
    servicesLoaded: true,
    fetchServices: vi.fn(),
  }),
}))
vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => ({
    isWorkspaceTransitioning: () => false,
    updateWorkspace: mocks.updateWorkspace,
    fetchWorkspaceDetail: mocks.fetchWorkspaceDetail,
  }),
}))
vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => ({
    runners: [],
    runnerById: () => mocks.runner,
    fetchRunners: mocks.fetchRunners,
  }),
}))
vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({
    success: mocks.success,
    error: mocks.error,
    warning: mocks.warning,
  }),
}))
vi.mock('@/stores/plugins', () => ({
  usePluginStore: () => ({
    plugins: [],
    loading: false,
    error: null,
    loadedOrgId: 'org-1',
    clear: vi.fn(),
    reload: vi.fn(async () => undefined),
  }),
}))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const selectionStub = {
  template: '<div data-testid="selection" />',
  props: ['pluginIds', 'credentialIds'],
}
function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'workspace-1',
    runner_id: 'runner-1',
    status: WorkspaceStatus.RUNNING,
    active_operation: null,
    name: 'Workspace',
    runtime_type: RuntimeType.QEMU,
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    last_activity_at: '2026-01-01',
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
    has_active_session: false,
    runner_online: true,
    credential_ids: ['cred-1'],
    plugin_ids: ['plugin-1'],
    credentials_present: true,
    ...overrides,
  }
}
function mountDialog(workspace = makeWorkspace(), resumeDraftId?: string) {
  setActivePinia(createPinia())
  const wrapper = mount(EditWorkspaceDialog, {
    props: { workspace, resumeDraftId },
    global: {
      stubs: {
        WorkspacePluginCredentialSelection: selectionStub,
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: { template: '<div><slot /></div>' },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        DialogBody: { template: '<div><slot /></div>' },
        DialogFooter: { template: '<div><slot /></div>' },
        DialogTrigger: { template: '<div><slot /></div>' },
      },
    },
  })
  const vm = wrapper.vm as unknown as {
    handleOpen: () => Promise<void>
    handleSubmit: () => Promise<void>
    name: string
    selectedPluginIds: string[]
    selectedCredentialIds: string[]
    qemuMemoryMb: number
    desktopWidth: number
    desktopHeight: number
    open: boolean
    pluginSelectionValid: boolean
  }
  return { wrapper, vm }
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.fetchCredentials.mockResolvedValue(undefined)
  mocks.fetchRunners.mockResolvedValue(undefined)
  mocks.fetchWorkspaceDetail.mockResolvedValue(undefined)
  mocks.updateWorkspace.mockResolvedValue(true)
  mocks.runner = {
    qemu_default_vcpus: 2,
    qemu_default_memory_mb: 4096,
    qemu_default_disk_size_gb: 50,
    qemu_min_vcpus: 1,
    qemu_max_vcpus: 8,
    qemu_min_memory_mb: 1024,
    qemu_max_memory_mb: 16384,
    qemu_min_disk_size_gb: 20,
    qemu_max_disk_size_gb: 200,
  }
})

describe('EditWorkspaceDialog single-patch workspace configuration', () => {
  it('restores persisted plugin IDs and sends one final PATCH containing credentials, plugins and resource fields', async () => {
    const { vm } = mountDialog()
    await vm.handleOpen()
    expect(vm.selectedPluginIds).toEqual(['plugin-1'])
    vm.pluginSelectionValid = true
    vm.name = 'Renamed'
    vm.selectedPluginIds = ['plugin-1', 'plugin-2']
    vm.selectedCredentialIds = ['cred-2']
    vm.qemuMemoryMb = 8192
    vm.desktopWidth = 1280
    vm.desktopHeight = 720
    await nextTick()
    await vm.handleSubmit()
    expect(mocks.updateWorkspace).toHaveBeenCalledTimes(1)
    expect(mocks.updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      name: 'Renamed',
      credential_ids: ['cred-2'],
      plugin_ids: ['plugin-1', 'plugin-2'],
      qemu_memory_mb: 8192,
      desktop_width: 1280,
      desktop_height: 720,
    })
    expect(vm.open).toBe(false)
  })

  it('restores edit fields and selected references after a new component is mounted', async () => {
    sessionStorage.clear()
    const saved = saveWorkspaceDraft(
      {
        mode: 'edit',
        workspaceId: 'workspace-1',
        name: 'Restored edit',
        credentialIds: ['cred-2'],
        pluginIds: ['plugin-2'],
        qemuVcpus: 4,
        qemuMemoryMb: 8192,
        qemuDiskSizeGb: 60,
        desktopWidth: 1600,
        desktopHeight: 900,
      },
      { userId: 1, organizationId: 'org-1' },
      '/workspaces/workspace-1',
    )
    const { vm } = mountDialog(makeWorkspace(), saved.id)
    await nextTick()
    expect(vm.open).toBe(true)
    expect(vm.name).toBe('Restored edit')
    expect(vm.selectedCredentialIds).toEqual(['cred-2'])
    expect(vm.selectedPluginIds).toEqual(['plugin-2'])
    expect(vm.qemuMemoryMb).toBe(8192)
    expect(vm.desktopWidth).toBe(1600)
  })

  it('submits no patch while shared selector has unsatisfied required dependencies', async () => {
    const { wrapper, vm } = mountDialog()
    await vm.handleOpen()
    vm.pluginSelectionValid = false
    await vm.handleSubmit()
    expect(mocks.updateWorkspace).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="edit-workspace-save"]').attributes('disabled')).toBeDefined()
  })

  it('keeps the edit dialog open when the single backend update fails', async () => {
    mocks.updateWorkspace.mockResolvedValue(false)
    const { vm } = mountDialog()
    await vm.handleOpen()
    vm.pluginSelectionValid = true
    vm.name = 'Failed update'
    await vm.handleSubmit()
    expect(mocks.updateWorkspace).toHaveBeenCalledTimes(1)
    expect(vm.open).toBe(true)
  })
})
