import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import CreateWorkspaceFromImageArtifactDialog from './CreateWorkspaceFromImageArtifactDialog.vue'
import type { ImageArtifact } from '@/types'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  clone: vi.fn(),
  notices: { error: vi.fn(), success: vi.fn() },
  auth: { initialized: true, user: { id: 1 }, activeOrganizationId: 'org-1' },
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/workspaces', query: {} }),
  useRouter: () => ({ push: mocks.push }),
}))
vi.mock('@/stores/imageArtifacts', () => ({
  useImageArtifactStore: () => ({
    loading: false,
    fetchImageArtifacts: vi.fn(),
    createWorkspaceFromImageArtifact: mocks.clone,
  }),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('@/stores/notifications', () => ({ useNotificationStore: () => mocks.notices }))
vi.mock('@/stores/plugins', () => ({
  usePluginStore: () => ({
    plugins: [],
    error: null,
    loading: false,
    loadedOrgId: 'org-1',
    reload: vi.fn(),
    clear: vi.fn(),
  }),
}))
vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({
    credentials: [],
    error: null,
    loading: false,
    servicesLoaded: true,
    fetchCredentials: vi.fn(),
    fetchServices: vi.fn(),
  }),
}))

const imageArtifact: ImageArtifact = {
  id: 'artifact-1',
  source_workspace_id: 'source-1',
  runner_artifact_id: 'runner-artifact',
  name: 'Captured',
  size_bytes: 10,
  status: 'ready',
  artifact_kind: 'captured',
  source_runner_id: 'runner-1',
  runtime_type: 'docker',
  source_runner_online: true,
  created_at: '',
  created_by_id: 1,
}

const stubs = {
  WorkspacePluginCredentialSelection: {
    template: '<div />',
    props: ['pluginIds', 'credentialIds'],
  },
  Dialog: { template: '<div><slot /></div>' },
  DialogTrigger: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mocks.clone.mockResolvedValue('new-workspace')
})

describe('CreateWorkspaceFromImageArtifactDialog', () => {
  it('submits selected plugin and credential IDs to the image-clone API without inheriting artifact associations', async () => {
    const wrapper = mount(CreateWorkspaceFromImageArtifactDialog, {
      props: { imageArtifact },
      global: { stubs },
    })
    const vm = wrapper.vm as unknown as {
      open: boolean
      name: string
      selectedCredentialIds: string[]
      selectedPluginIds: string[]
      pluginSelectionValid: boolean
      handleSubmit: () => Promise<void>
    }
    vm.open = true
    vm.name = 'Image clone'
    vm.selectedCredentialIds = ['credential-1']
    vm.selectedPluginIds = ['plugin-1']
    vm.pluginSelectionValid = true
    await vm.handleSubmit()
    expect(mocks.clone).toHaveBeenCalledWith('artifact-1', {
      name: 'Image clone',
      credential_ids: ['credential-1'],
      plugin_ids: ['plugin-1'],
    })
    expect(mocks.push).toHaveBeenCalledWith('/workspaces/new-workspace')
  })

  it('does not dispatch clone creation while required dependencies are missing', async () => {
    const wrapper = mount(CreateWorkspaceFromImageArtifactDialog, {
      props: { imageArtifact },
      global: { stubs },
    })
    const vm = wrapper.vm as unknown as {
      open: boolean
      name: string
      pluginSelectionValid: boolean
      handleSubmit: () => Promise<void>
    }
    vm.open = true
    vm.name = 'Blocked clone'
    vm.pluginSelectionValid = false
    await vm.handleSubmit()
    await flushPromises()
    expect(mocks.clone).not.toHaveBeenCalled()
  })
})
