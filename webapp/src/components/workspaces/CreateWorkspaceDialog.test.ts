import { nextTick } from 'vue'
import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import CreateWorkspaceDialog from './CreateWorkspaceDialog.vue'
import { saveWorkspaceDraft } from '@/lib/workspaceDraft'

const routerPush = vi.fn()
const fetchCredentials = vi.fn()
const fetchRunners = vi.fn()
const fetchCapturedImages = vi.fn()
const fetchImageDefinitionsWithBuilds = vi.fn()
const createWorkspace = vi.fn()
const createWorkspaceFromCapturedImage = vi.fn()

const credentialStore = {
  credentials: [
    {
      id: 'cred-1',
      name: 'GitHub Token',
      service_id: 'service-github',
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
  workspaces: [],
  createWorkspace,
}

const imageStore = {
  capturedImages: [
    {
      id: 'image-line-1',
      name: 'Captured Workspace',
      status: 'active',
      latest_id: 'captured-image-1',
      latest_version: 1,
      runner_online: true,
      versions: [
        {
          id: 'captured-image-1',
          source_workspace_id: 'workspace-1',
          runner_artifact_id: 'artifact-1',
          name: 'Captured Workspace',
          size_bytes: 123,
          status: 'ready',
          artifact_kind: 'captured',
          source_runner_id: 'runner-1',
          source_runner_online: true,
          created_at: '2026-04-01T10:00:00.000Z',
          created_by_id: 1,
        },
      ],
    },
  ],
  imageDefinitions: [] as Record<string, unknown>[],
  runnerBuildsByDefinition: {} as Record<string, Record<string, unknown>[]>,
  fetchCapturedImages,
  fetchImageDefinitionsWithBuilds,
  createWorkspaceFromCapturedImage,
}

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
  useRoute: () => ({ path: '/', query: {} }),
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
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 1 }, activeOrganizationId: 'org-1', initialized: true }),
}))
vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  }),
}))

describe('CreateWorkspaceDialog', () => {
  beforeEach(() => {
    routerPush.mockReset()
    fetchCredentials.mockReset()
    fetchRunners.mockReset()
    fetchCapturedImages.mockReset()
    fetchImageDefinitionsWithBuilds.mockReset()
    createWorkspace.mockReset()
    createWorkspaceFromCapturedImage.mockReset()
    createWorkspace.mockResolvedValue(true)
  })

  it('restores a create configuration into a newly mounted dialog after the credentials redirect', async () => {
    const saved = saveWorkspaceDraft(
      {
        mode: 'create',
        name: 'Restored configuration',
        credentialIds: ['cred-1'],
        pluginIds: ['plugin-1'],
        repos: ['https://example.test/repo'],
        runnerId: 'runner-1',
        qemuVcpus: 3,
        qemuMemoryMb: 8192,
        qemuDiskSizeGb: 75,
        imageValue: 'captured:captured-image-1',
      },
      { userId: 1, organizationId: 'org-1' },
      '/workspaces',
    )
    const wrapper = shallowMount(CreateWorkspaceDialog, { props: { resumeDraftId: saved.id } })
    await nextTick()
    const vm = wrapper.vm as typeof wrapper.vm & {
      open: boolean
      name: string
      selectedCredentialIds: string[]
      selectedPluginIds: string[]
      repos: string[]
      runnerId: string
      qemuMemoryMb: number
      selectedImageValue: string
    }
    expect(vm.open).toBe(true)
    expect(vm.name).toBe('Restored configuration')
    expect(vm.selectedCredentialIds).toEqual(['cred-1'])
    expect(vm.selectedPluginIds).toEqual(['plugin-1'])
    expect(vm.repos).toEqual(['https://example.test/repo'])
    expect(vm.runnerId).toBe('runner-1')
    expect(vm.qemuMemoryMb).toBe(8192)
    expect(vm.selectedImageValue).toBe('captured:captured-image-1')
  })

  it('creates a workspace from a captured image via its versioned image ID', async () => {
    const wrapper = shallowMount(CreateWorkspaceDialog)
    const vm = wrapper.vm as typeof wrapper.vm & {
      open: boolean
      name: string
      selectedImageValue: string
      selectedCredentialIds: string[]
      pluginSelectionValid: boolean
      handleSubmit: () => Promise<void>
    }

    createWorkspaceFromCapturedImage.mockResolvedValue('workspace-created-from-image')
    vm.open = true
    vm.name = 'Captured Clone'
    vm.selectedImageValue = 'captured:captured-image-1'
    await nextTick()
    vm.selectedCredentialIds = ['cred-1']
    vm.pluginSelectionValid = true
    await nextTick()

    await vm.handleSubmit()

    expect(createWorkspaceFromCapturedImage).toHaveBeenCalledWith('image-line-1', {
      name: 'Captured Clone',
      credential_ids: ['cred-1'],
      plugin_ids: [],
    })
    expect(vm.selectedCredentialIds).toEqual(['cred-1'])
    expect(createWorkspace).not.toHaveBeenCalled()
  })

  it('creates with the image runner defaults when resources are left untouched', async () => {
    const runner = runnerStore.runners[0]!
    const original = { ...runner }
    Object.assign(runner, {
      qemu_max_vcpus: 4,
      qemu_default_vcpus: 1,
      qemu_max_memory_mb: 8192,
      qemu_default_memory_mb: 1024,
      qemu_max_disk_size_gb: 100,
      qemu_default_disk_size_gb: 20,
    })
    imageStore.imageDefinitions = [
      { id: 'definition-1', name: 'Ubuntu', is_active: true, status: 'active' },
    ]
    imageStore.runnerBuildsByDefinition = {
      'definition-1': [
        {
          id: 'build-1',
          image_definition_id: 'definition-1',
          runner_id: 'runner-1',
          image_artifact_id: 'artifact-1',
          status: 'active',
        },
      ],
    }
    try {
      const wrapper = shallowMount(CreateWorkspaceDialog)
      const vm = wrapper.vm as typeof wrapper.vm & {
        open: boolean
        name: string
        pluginSelectionValid: boolean
        handleSubmit: () => Promise<void>
      }
      vm.open = true
      vm.name = 'Small runner'
      vm.pluginSelectionValid = true
      await nextTick()

      await vm.handleSubmit()

      expect(createWorkspace).toHaveBeenCalledWith(
        expect.objectContaining({
          runner_id: 'runner-1',
          image_id: 'artifact-1',
          qemu_vcpus: 1,
          qemu_memory_mb: 1024,
          qemu_disk_size_gb: 20,
        }),
      )
    } finally {
      Object.assign(runner, original)
      imageStore.imageDefinitions = []
      imageStore.runnerBuildsByDefinition = {}
    }
  })
})
