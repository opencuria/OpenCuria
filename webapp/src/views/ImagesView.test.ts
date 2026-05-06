import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ImagesView from './ImagesView.vue'
import type { ImageArtifact } from '@/types'

const startPolling = vi.fn()

const imageStore = {
  images: [] as ImageArtifact[],
  loading: false,
  error: null,
  fetchImages: vi.fn(),
  deleteImageArtifact: vi.fn(),
  renameImageArtifact: vi.fn(),
}

vi.mock('@/stores/images', () => ({
  useImageStore: () => imageStore,
}))

vi.mock('@/composables/usePolling', () => ({
  usePolling: () => ({
    start: startPolling,
  }),
}))

describe('ImagesView', () => {
  beforeEach(() => {
    startPolling.mockReset()
    imageStore.fetchImages.mockReset()
    imageStore.deleteImageArtifact.mockReset()
    imageStore.renameImageArtifact.mockReset()
    imageStore.loading = false
    imageStore.error = null
    imageStore.images = []
  })

  it('shows captured images with backend capturing status as in progress', () => {
    imageStore.images = [
      {
        id: 'captured-image-1',
        source_workspace_id: 'workspace-1',
        runner_artifact_id: '',
        name: 'Snapshot',
        size_bytes: 1024,
        status: 'capturing',
        artifact_kind: 'captured',
        runtime_type: 'qemu',
        is_deactivated: false,
        source_runner_online: true,
        delete_requested_at: null,
        delete_confirmed_at: null,
        delete_last_error: '',
        created_at: '2026-05-06T12:00:00.000Z',
        created_by_id: 1,
      },
    ]

    const wrapper = mount(ImagesView, {
      global: {
        stubs: {
          UiSpinner: { template: '<div />' },
          UiButton: { template: '<button><slot /></button>' },
          UiCard: { template: '<div><slot /></div>' },
          UiCardContent: { template: '<div><slot /></div>' },
          UiBadge: { template: '<span><slot /></span>' },
          CreateImageArtifactDialog: { template: '<button>Capture Image</button>' },
          CreateWorkspaceFromImageArtifactDialog: { template: '<div><slot /></div>' },
        },
      },
    })

    expect(wrapper.text()).toContain('Creating…')
    expect(wrapper.text()).not.toContain('Clone Workspace')
  })
})
