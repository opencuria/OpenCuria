import { nextTick } from 'vue'
import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import CreateWorkspaceDialog from './CreateWorkspaceDialog.vue'
import { RuntimeType } from '@/types'

const routerPush = vi.fn()
const fetchCredentials = vi.fn()
const fetchRunners = vi.fn()
const fetchImages = vi.fn()
const fetchImageDefinitionsWithBuilds = vi.fn()
const createWorkspace = vi.fn()
const createWorkspaceFromImageArtifact = vi.fn()

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
      name: 'Runner 1',
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
  get onlineRunners() {
    return this.runners.filter((runner) => runner.status === 'online')
  },
  fetchRunners,
}

const workspaceStore = {
  createWorkspace,
}

const imageStore = {
  images: [
    {
      id: 'captured-image-1',
      source_workspace_id: 'workspace-1',
      runner_artifact_id: 'artifact-1',
      name: 'Captured Workspace',
      size_bytes: 123,
      status: 'ready',
      artifact_kind: 'captured',
      source_runner_id: 'runner-1',
      runtime_type: RuntimeType.QEMU,
      source_runner_online: true,
      created_at: '2026-04-01T10:00:00.000Z',
      created_by_id: 1,
    },
  ],
  imageDefinitions: [],
  runnerBuildsByDefinition: {},
  fetchImages,
  fetchImageDefinitionsWithBuilds,
  createWorkspaceFromImageArtifact,
}

vi.mock('vue-router', () => ({
  useRouter: () => ({
    push: routerPush,
  }),
}))

vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => credentialStore,
}))

vi.mock('@/stores/runners', () => ({
  useRunnerStore: () => runnerStore,
}))

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/images', () => ({
  useImageStore: () => imageStore,
}))

describe('CreateWorkspaceDialog', () => {
  beforeEach(() => {
    routerPush.mockReset()
    fetchCredentials.mockReset()
    fetchRunners.mockReset()
    fetchImages.mockReset()
    fetchImageDefinitionsWithBuilds.mockReset()
    createWorkspace.mockReset()
    createWorkspaceFromImageArtifact.mockReset()
    createWorkspace.mockResolvedValue(true)
  })

  it('creates a workspace from a captured image via the artifact clone flow', async () => {
    const wrapper = shallowMount(CreateWorkspaceDialog)
    const vm = wrapper.vm as typeof wrapper.vm & {
      open: boolean
      name: string
      selectedImageValue: string
      selectedCredentialIds: string[]
      handleSubmit: () => Promise<void>
    }

    createWorkspaceFromImageArtifact.mockResolvedValue('workspace-created-from-image')
    vm.open = true
    vm.name = 'Captured Clone'
    vm.selectedImageValue = 'captured:captured-image-1'
    vm.selectedCredentialIds = ['cred-1']
    await nextTick()

    await vm.handleSubmit()

    expect(createWorkspaceFromImageArtifact).toHaveBeenCalledWith('captured-image-1', {
      name: 'Captured Clone',
      credential_ids: ['cred-1'],
    })
    expect(createWorkspace).not.toHaveBeenCalled()
  })
})
