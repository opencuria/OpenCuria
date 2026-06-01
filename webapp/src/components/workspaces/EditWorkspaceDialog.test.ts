import { nextTick } from 'vue'
import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import EditWorkspaceDialog from './EditWorkspaceDialog.vue'
import { RuntimeType, WorkspaceStatus, type Workspace } from '@/types'

const workspacesApiMocks = vi.hoisted(() => ({
  listDesktopStartCommands: vi.fn(),
  createDesktopStartCommand: vi.fn(),
  updateDesktopStartCommand: vi.fn(),
  deleteDesktopStartCommand: vi.fn(),
}))

const routerPush = vi.fn()
const fetchCredentials = vi.fn()
const fetchRunners = vi.fn()
const updateWorkspace = vi.fn()
const notificationError = vi.fn()

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

const workspaceStore = {
  updateWorkspace,
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

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({
    error: notificationError,
  }),
}))

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => runnerStore,
}))

vi.mock('@/services/workspaces.api', async () => {
  const actual = await vi.importActual<typeof import('@/services/workspaces.api')>('@/services/workspaces.api')
  return {
    ...actual,
    listDesktopStartCommands: workspacesApiMocks.listDesktopStartCommands,
    createDesktopStartCommand: workspacesApiMocks.createDesktopStartCommand,
    updateDesktopStartCommand: workspacesApiMocks.updateDesktopStartCommand,
    deleteDesktopStartCommand: workspacesApiMocks.deleteDesktopStartCommand,
  }
})

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
  }
}

describe('EditWorkspaceDialog', () => {
  beforeEach(() => {
    routerPush.mockReset()
    fetchCredentials.mockReset()
    fetchRunners.mockReset()
    updateWorkspace.mockReset()
    notificationError.mockReset()
    workspacesApiMocks.listDesktopStartCommands.mockReset()
    workspacesApiMocks.createDesktopStartCommand.mockReset()
    workspacesApiMocks.updateDesktopStartCommand.mockReset()
    workspacesApiMocks.deleteDesktopStartCommand.mockReset()
    updateWorkspace.mockResolvedValue(true)
    workspacesApiMocks.listDesktopStartCommands.mockResolvedValue([])
  })

  it('omits unchanged QEMU resources when saving non-resource edits', async () => {
    const wrapper = shallowMount(EditWorkspaceDialog, {
      props: {
        workspace: makeWorkspace(),
      },
    })

    await (wrapper.vm as typeof wrapper.vm & { handleOpen: () => Promise<void> }).handleOpen()
    ;(wrapper.vm as typeof wrapper.vm & { name: string }).name = 'Renamed workspace'
    await nextTick()

    await (wrapper.vm as typeof wrapper.vm & { handleSubmit: () => Promise<void> }).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      name: 'Renamed workspace',
      credential_ids: ['cred-1'],
    })
  })

  it('includes only the QEMU resource fields that changed', async () => {
    const wrapper = shallowMount(EditWorkspaceDialog, {
      props: {
        workspace: makeWorkspace(),
      },
    })

    await (wrapper.vm as typeof wrapper.vm & { handleOpen: () => Promise<void> }).handleOpen()
    ;(wrapper.vm as typeof wrapper.vm & { qemuMemoryMb: number }).qemuMemoryMb = 8192
    await nextTick()

    await (wrapper.vm as typeof wrapper.vm & { handleSubmit: () => Promise<void> }).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      name: 'Workspace',
      credential_ids: ['cred-1'],
      qemu_memory_mb: 8192,
    })
  })

  it('replaces the selected credential when another credential from the same service is chosen', async () => {
    const wrapper = shallowMount(EditWorkspaceDialog, {
      props: {
        workspace: makeWorkspace(),
      },
    })

    await (wrapper.vm as typeof wrapper.vm & { handleOpen: () => Promise<void> }).handleOpen()
    ;(wrapper.vm as typeof wrapper.vm & { toggleCredential: (id: string) => void }).toggleCredential('cred-2')

    expect(
      (wrapper.vm as typeof wrapper.vm & { selectedCredentialIds: string[] }).selectedCredentialIds,
    ).toEqual(['cred-2'])
  })

  it('creates, updates, and deletes desktop start commands on save', async () => {
    workspacesApiMocks.listDesktopStartCommands.mockResolvedValue([
      {
        id: 'cmd-existing',
        workspace_id: 'workspace-1',
        name: 'Browser',
        command: '/usr/local/bin/opencuria-desktop-browser',
        created_at: '2026-04-01T10:00:00.000Z',
        updated_at: '2026-04-01T10:00:00.000Z',
      },
      {
        id: 'cmd-delete',
        workspace_id: 'workspace-1',
        name: 'Delete me',
        command: 'echo delete',
        created_at: '2026-04-01T10:00:00.000Z',
        updated_at: '2026-04-01T10:00:00.000Z',
      },
    ])

    const wrapper = shallowMount(EditWorkspaceDialog, {
      props: {
        workspace: makeWorkspace(),
      },
    })

    await (wrapper.vm as typeof wrapper.vm & { handleOpen: () => Promise<void> }).handleOpen()

    const desktopCommands = (
      wrapper.vm as typeof wrapper.vm & {
        desktopStartCommands: Array<{ id: string; localId: string; name: string; command: string }>
      }
    ).desktopStartCommands
    expect(desktopCommands[0]).toBeDefined()
    desktopCommands[0]!.name = 'Browser Updated'
    desktopCommands.splice(1, 1)
    ;(wrapper.vm as typeof wrapper.vm & { addDesktopStartCommand: () => void }).addDesktopStartCommand()
    expect(desktopCommands[1]).toBeDefined()
    desktopCommands[1]!.name = 'Docs'
    desktopCommands[1]!.command = 'xdg-open https://docs.example.test'

    await (wrapper.vm as typeof wrapper.vm & { handleSubmit: () => Promise<void> }).handleSubmit()

    expect(updateWorkspace).toHaveBeenCalled()
    expect(workspacesApiMocks.updateDesktopStartCommand).toHaveBeenCalledWith('workspace-1', 'cmd-existing', {
      name: 'Browser Updated',
    })
    expect(workspacesApiMocks.deleteDesktopStartCommand).toHaveBeenCalledWith('workspace-1', 'cmd-delete')
    expect(workspacesApiMocks.createDesktopStartCommand).toHaveBeenCalledWith('workspace-1', {
      name: 'Docs',
      command: 'xdg-open https://docs.example.test',
    })
  })
})
