import { nextTick } from 'vue'
import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import EditWorkspaceDialog from './EditWorkspaceDialog.vue'
import { RuntimeType, WorkspaceStatus, type Workspace } from '@/types'

const routerPush = vi.fn()
const fetchCredentials = vi.fn()
const fetchRunners = vi.fn()
const updateWorkspace = vi.fn()

const credentialStore = {
  credentials: [
    {
      id: 'cred-1',
      name: 'GitHub Token',
      credential_type: 'env_var',
      env_var_name: 'GITHUB_TOKEN',
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

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => runnerStore,
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
    created_by_id: overrides.created_by_id ?? 1,
    last_activity_at: overrides.last_activity_at ?? '2026-04-01T10:00:00.000Z',
    auto_stop_timeout_minutes: overrides.auto_stop_timeout_minutes ?? null,
    auto_stop_at: overrides.auto_stop_at ?? null,
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
    updateWorkspace.mockResolvedValue(true)
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
})
