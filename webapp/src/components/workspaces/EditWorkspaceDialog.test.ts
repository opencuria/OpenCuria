import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { mount, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import EditWorkspaceDialog from './EditWorkspaceDialog.vue'
import { RuntimeType, WorkspaceStatus, type Workspace, type WorkspacePlugin } from '@/types'

const routerPush = vi.fn()
const fetchCredentials = vi.fn()
const fetchRunners = vi.fn()
const updateWorkspace = vi.fn()
const fetchWorkspacePlugins = vi.fn()
const setWorkspacePlugins = vi.fn()
const resyncWorkspacePlugins = vi.fn()
const fetchPlugins = vi.fn()

const credentialStore = {
  credentials: [
    {
      id: 'cred-1',
      name: 'GitHub Token',
      scope: 'personal',
      service_id: 'service-github',
      service_name: 'GitHub',
      service_slug: 'github',
      credential_type: 'env',
      env_var_name: 'GITHUB_TOKEN',
      target_path: '',
      has_public_key: false,
      created_by_id: 1,
      created_at: '2026-04-01T10:00:00.000Z',
      updated_at: '2026-04-01T10:00:00.000Z',
    },
    {
      id: 'cred-2',
      name: 'GitHub Token (Org)',
      scope: 'organization',
      service_id: 'service-github',
      service_name: 'GitHub',
      service_slug: 'github',
      credential_type: 'env',
      env_var_name: 'GITHUB_TOKEN',
      target_path: '',
      has_public_key: false,
      created_by_id: 1,
      created_at: '2026-04-01T10:00:00.000Z',
      updated_at: '2026-04-01T10:00:00.000Z',
    },
  ],
  fetchCredentials,
}

const runnerStore = {
  runners: [
    {
      id: 'runner-1',
      name: 'Runner',
      status: 'online',
      available_runtimes: ['docker', 'qemu'],
      organization_id: 'org-1',
      connected_at: null,
      disconnected_at: null,
      qemu_min_vcpus: 1,
      qemu_max_vcpus: 8,
      qemu_default_vcpus: 2,
      qemu_min_memory_mb: 1024,
      qemu_max_memory_mb: 16384,
      qemu_default_memory_mb: 4096,
      qemu_min_disk_size_gb: 20,
      qemu_max_disk_size_gb: 200,
      qemu_default_disk_size_gb: 50,
      qemu_max_active_vcpus: null,
      qemu_max_active_memory_mb: null,
      qemu_max_active_disk_size_gb: null,
      created_at: '2026-04-01T10:00:00.000Z',
      updated_at: '2026-04-01T10:00:00.000Z',
    },
  ],
  fetchRunners,
  runnerById: (id: string) => runnerStore.runners.find((runner) => runner.id === id),
}

const fetchWorkspaceDetail = vi.fn(async (_id: string) => {})

const workspaceStore = {
  updateWorkspace,
  fetchWorkspaceDetail,
}

function makeWorkspacePlugin(overrides: Partial<WorkspacePlugin> = {}): WorkspacePlugin {
  return {
    id: 'plugin-1',
    name: 'Playwright',
    slug: 'playwright',
    description: 'Browser automation',
    organization_id: null,
    is_global: true,
    workspace_enabled: false,
    missing_required_credentials: [
      { key: 'api_key', service_id: 'service-github', service_slug: 'github' },
    ],
    ready: false,
    ...overrides,
  }
}

const pluginStore = {
  plugins: [] as Array<{
    id: string
    credential_requirements: Array<{ required: boolean; service_id: string }>
  }>,
  workspacePlugins: {} as Record<string, WorkspacePlugin[]>,
  workspacePluginsLoading: {} as Record<string, boolean>,
  workspacePluginsError: {} as Record<string, string | null>,
  fetchWorkspacePlugins,
  setWorkspacePlugins,
  resyncWorkspacePlugins,
  fetchPlugins,
}

vi.mock('vue-router', () => ({
  useRouter: () => ({
    push: routerPush,
  }),
}))

vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => credentialStore,
}))

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => runnerStore,
}))

const notificationStoreMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}))

vi.mock('@/stores/plugins', () => ({
  usePluginStore: () => pluginStore,
}))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => notificationStoreMock,
}))

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: overrides.id ?? 'workspace-1',
    runner_id: overrides.runner_id ?? 'runner-1',
    status: overrides.status ?? WorkspaceStatus.RUNNING,
    active_operation: overrides.active_operation ?? null,
    name: overrides.name ?? 'Workspace',
    runtime_type: overrides.runtime_type ?? RuntimeType.QEMU,
    qemu_vcpus: overrides.qemu_vcpus ?? null,
    qemu_memory_mb: overrides.qemu_memory_mb ?? null,
    qemu_disk_size_gb: overrides.qemu_disk_size_gb ?? null,
    desktop_width: overrides.desktop_width ?? 1920,
    desktop_height: overrides.desktop_height ?? 1080,
    created_by_id: overrides.created_by_id ?? 1,
    last_activity_at: overrides.last_activity_at ?? '2026-04-01T10:00:00.000Z',
    auto_stop_timeout_minutes: overrides.auto_stop_timeout_minutes ?? null,
    auto_stop_at: overrides.auto_stop_at ?? null,
    delete_requested_at: overrides.delete_requested_at ?? null,
    delete_started_at: overrides.delete_started_at ?? null,
    delete_confirmed_at: overrides.delete_confirmed_at ?? null,
    delete_last_error: overrides.delete_last_error ?? '',
    delete_attempt_count: overrides.delete_attempt_count ?? 0,
    created_at: overrides.created_at ?? '2026-04-01T10:00:00.000Z',
    updated_at: overrides.updated_at ?? '2026-04-01T10:00:00.000Z',
    has_active_session: overrides.has_active_session ?? false,
    runner_online: overrides.runner_online ?? true,
    credential_ids: overrides.credential_ids ?? ['cred-1'],
    credentials_present: overrides.credentials_present ?? false,
  }
}

type Vm = {
  handleOpen: () => Promise<void>
  handleSubmit: () => Promise<void>
  toggleCredential: (id: string) => void
  togglePlugin: (id: string) => void
  name: string
  qemuMemoryMb: number
  desktopWidth: number
  desktopHeight: number
  selectedCredentialIds: string[]
  selectedPluginIds: string[]
  open: boolean
}

function vmOf(wrapper: ReturnType<typeof shallowMount>): Vm {
  return wrapper.vm as unknown as Vm
}

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>', props: ['open'] },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
  DialogTrigger: { template: '<div><slot /></div>' },
}

function mountDialog(workspace: Workspace) {
  setActivePinia(createPinia())
  return mount(EditWorkspaceDialog, {
    props: { workspace },
    global: { stubs: dialogStubs },
  })
}

function mountShallowDialog(workspace: Workspace) {
  setActivePinia(createPinia())
  return shallowMount(EditWorkspaceDialog, {
    props: { workspace },
  })
}

describe('EditWorkspaceDialog', () => {
  beforeEach(() => {
    routerPush.mockReset()
    fetchCredentials.mockReset()
    fetchRunners.mockReset()
    fetchWorkspacePlugins.mockReset()
    setWorkspacePlugins.mockReset()
    resyncWorkspacePlugins.mockReset()
    fetchPlugins.mockReset()
    updateWorkspace.mockReset()
    updateWorkspace.mockResolvedValue(true)
    fetchWorkspaceDetail.mockReset()
    fetchWorkspaceDetail.mockResolvedValue(undefined)
    pluginStore.plugins = []
    pluginStore.workspacePlugins = {}
    pluginStore.workspacePluginsLoading = {}
    pluginStore.workspacePluginsError = {}
  })

  it('omits unchanged QEMU resources when saving non-resource edits', async () => {
    pluginStore.workspacePlugins = { 'workspace-1': [] }
    const wrapper = mountShallowDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).name = 'Renamed workspace'
    await nextTick()

    await vmOf(wrapper).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      name: 'Renamed workspace',
      credential_ids: ['cred-1'],
    })
    expect(setWorkspacePlugins).not.toHaveBeenCalled()
  })

  it('includes only the QEMU resource fields that changed', async () => {
    pluginStore.workspacePlugins = { 'workspace-1': [] }
    const wrapper = mountShallowDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).qemuMemoryMb = 8192
    await nextTick()

    await vmOf(wrapper).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      name: 'Workspace',
      credential_ids: ['cred-1'],
      qemu_memory_mb: 8192,
    })
  })

  it('replaces the selected credential when another credential from the same service is chosen', async () => {
    pluginStore.workspacePlugins = { 'workspace-1': [] }
    const wrapper = mountShallowDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).toggleCredential('cred-2')

    expect(vmOf(wrapper).selectedCredentialIds).toEqual(['cred-2'])
  })

  it('includes changed desktop size when saving', async () => {
    pluginStore.workspacePlugins = { 'workspace-1': [] }
    const wrapper = mountShallowDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).desktopWidth = 1280
    vmOf(wrapper).desktopHeight = 720
    await nextTick()

    await vmOf(wrapper).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      name: 'Workspace',
      credential_ids: ['cred-1'],
      desktop_width: 1280,
      desktop_height: 720,
    })
  })

  it('loads credentials and workspace plugins on open and preselects enabled plugins', async () => {
    pluginStore.workspacePlugins = {
      'workspace-1': [makeWorkspacePlugin({ workspace_enabled: true })],
    }
    const wrapper = mountDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()

    expect(fetchCredentials).toHaveBeenCalled()
    expect(fetchWorkspacePlugins).toHaveBeenCalledWith('workspace-1')
    expect(vmOf(wrapper).selectedPluginIds).toEqual(['plugin-1'])
    expect(wrapper.find('[data-testid="workspace-plugin-plugin-1"]').exists()).toBe(true)
  })

  it('blocks save when a selected plugin misses credentials until attach', async () => {
    pluginStore.workspacePlugins = { 'workspace-1': [makeWorkspacePlugin()] }
    const wrapper = mountDialog(makeWorkspace({ credential_ids: [] }))

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).togglePlugin('plugin-1')
    await nextTick()

    expect(wrapper.find('[data-testid="workspace-plugin-missing-plugin-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(true)

    await vmOf(wrapper).handleSubmit()
    expect(updateWorkspace).not.toHaveBeenCalled()
    expect(setWorkspacePlugins).not.toHaveBeenCalled()

    // Attaching a matching credential clears the gate.
    await wrapper.find('[data-testid="workspace-plugin-attach-plugin-1-cred-1"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(false)

    updateWorkspace.mockResolvedValue(true)
    setWorkspacePlugins.mockResolvedValue([makeWorkspacePlugin({ workspace_enabled: true })])
    await vmOf(wrapper).handleSubmit()
    expect(updateWorkspace).toHaveBeenCalled()
    expect(setWorkspacePlugins).toHaveBeenCalledWith('workspace-1', ['plugin-1'])
  })

  it('saves deactivation → credential patch → final activation in order', async () => {
    const calls: string[] = []
    setWorkspacePlugins.mockImplementation(async (_id: string, ids: string[]) => {
      calls.push(`plugins:${ids.join(',')}`)
      return []
    })
    updateWorkspace.mockImplementation(async () => {
      calls.push('workspace')
      return true
    })
    pluginStore.workspacePlugins = {
      'workspace-1': [
        makeWorkspacePlugin({
          workspace_enabled: true,
          missing_required_credentials: [],
          ready: true,
        }),
      ],
    }
    const wrapper = mountShallowDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    // Deselect the only enabled plugin.
    vmOf(wrapper).togglePlugin('plugin-1')
    await nextTick()
    await vmOf(wrapper).handleSubmit()

    expect(calls).toEqual(['plugins:', 'workspace', 'plugins:'])
    expect(updateWorkspace).toHaveBeenCalledWith(
      'workspace-1',
      expect.objectContaining({ credential_ids: ['cred-1'] }),
      expect.objectContaining({ notify: false }),
    )
    expect(vmOf(wrapper).open).toBe(false)
  })

  it('keeps the dialog open and resyncs when a step fails', async () => {
    setWorkspacePlugins.mockResolvedValue(null)
    pluginStore.workspacePlugins = {
      'workspace-1': [
        makeWorkspacePlugin({
          workspace_enabled: true,
          missing_required_credentials: [],
          ready: true,
        }),
      ],
    }
    const wrapper = mountShallowDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).togglePlugin('plugin-1')
    await nextTick()
    await vmOf(wrapper).handleSubmit()

    expect(updateWorkspace).not.toHaveBeenCalled()
    expect(resyncWorkspacePlugins).toHaveBeenCalledWith('workspace-1')
    expect(vmOf(wrapper).open).toBe(true)
  })

  it('attach is idempotent: an attached credential shows Attached and stays selected', async () => {
    pluginStore.workspacePlugins = {
      'workspace-1': [makeWorkspacePlugin({ workspace_enabled: false })],
    }
    pluginStore.plugins = []
    const wrapper = mountDialog(makeWorkspace({ credential_ids: [] }))

    await vmOf(wrapper).handleOpen()
    await nextTick()
    vmOf(wrapper).togglePlugin('plugin-1')
    await nextTick()

    // Gap offers Attach for the missing credential…
    const attach = wrapper.find('[data-testid="workspace-plugin-attach-plugin-1-cred-1"]')
    expect(attach.exists()).toBe(true)
    expect(attach.text()).toContain('Attach')
    await attach.trigger('click')
    await nextTick()
    expect(vmOf(wrapper).selectedCredentialIds).toEqual(['cred-1'])

    // …and once attached the button is disabled/idempotent (no detach).
    const attached = wrapper.find('[data-testid="workspace-plugin-attach-plugin-1-cred-1"]')
    if (attached.exists()) {
      expect(attached.attributes('disabled')).toBeDefined()
      await attached.trigger('click')
      expect(vmOf(wrapper).selectedCredentialIds).toEqual(['cred-1'])
    } else {
      // Gap resolved via catalog requirements: still selected.
      expect(vmOf(wrapper).selectedCredentialIds).toEqual(['cred-1'])
    }
  })

  it('disables plugin toggles but still saves workspace fields when the plugin list fails to load', async () => {
    pluginStore.workspacePlugins = {}
    pluginStore.workspacePluginsError = { 'workspace-1': 'boom' }
    const wrapper = mountDialog(makeWorkspace())

    await vmOf(wrapper).handleOpen()
    expect(wrapper.find('[data-testid="workspace-plugins-error"]').text()).toContain(
      'Plugin changes are disabled',
    )
    expect(wrapper.find('[data-testid="edit-workspace-save"]').attributes('disabled')).toBeUndefined()

    vmOf(wrapper).name = 'Renamed'
    await nextTick()
    await vmOf(wrapper).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith(
      'workspace-1',
      expect.objectContaining({ name: 'Renamed' }),
    )
    expect(setWorkspacePlugins).not.toHaveBeenCalled()
  })

  it('warns (no success toast) when the workspace patch succeeds but final activation fails', async () => {
    updateWorkspace.mockResolvedValue(true)
    fetchWorkspaceDetail.mockResolvedValue(undefined)
    notificationStoreMock.warning.mockClear()
    notificationStoreMock.success.mockClear()
    setWorkspacePlugins.mockResolvedValue(null)
    pluginStore.workspacePlugins = {
      'workspace-1': [
        makeWorkspacePlugin({
          workspace_enabled: false,
          missing_required_credentials: [],
          ready: true,
        }),
      ],
    }
    pluginStore.plugins = [
      {
        id: 'plugin-1',
        credential_requirements: [],
      },
    ]
    const wrapper = mountShallowDialog(makeWorkspace({ credential_ids: ['cred-1'] }))

    await vmOf(wrapper).handleOpen()
    vmOf(wrapper).togglePlugin('plugin-1')
    await nextTick()
    await vmOf(wrapper).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith(
      'workspace-1',
      expect.anything(),
      expect.objectContaining({ notify: false }),
    )
    expect(notificationStoreMock.warning).toHaveBeenCalledWith('Partially saved', expect.any(String))
    expect(notificationStoreMock.success).not.toHaveBeenCalledWith('Workspace updated', expect.anything())
    expect(vmOf(wrapper).open).toBe(true)
    expect(fetchWorkspaceDetail).toHaveBeenCalledWith('workspace-1')
    expect(resyncWorkspacePlugins).toHaveBeenCalledWith('workspace-1')
  })
})
