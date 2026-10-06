import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ImagesView from './ImagesView.vue'
import type { CapturedImage, ImageArtifact } from '@/types'

const startPolling = vi.fn()

const imageStore = {
  capturedImages: [] as CapturedImage[],
  loading: false,
  error: null,
  fetchCapturedImages: vi.fn(),
  renameCapturedImage: vi.fn(),
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
    imageStore.fetchCapturedImages.mockReset()
    imageStore.renameCapturedImage.mockReset()
    imageStore.loading = false
    imageStore.error = null
    imageStore.capturedImages = []
  })

  it('shows captured images with backend capturing status as in progress', () => {
    const capturing: ImageArtifact = {
      id: 'captured-image-1',
      source_workspace_id: 'workspace-1',
      runner_artifact_id: '',
      name: 'Snapshot',
      size_bytes: null,
      status: 'capturing',
      artifact_kind: 'captured',
      runtime_type: 'qemu',
      created_at: '2026-05-06T12:00:00.000Z',
      created_by_id: 1,
      captured_image_id: 'line-1',
      version: 1,
    }
    imageStore.capturedImages = [
      {
        id: 'line-1',
        name: 'Snapshot',
        status: 'active',
        runner_id: 'r',
        runner_online: true,
        created_by_id: 1,
        created_at: '2026-05-06T12:00:00.000Z',
        latest_id: null,
        latest_version: null,
        total_size_bytes: 0,
        workspace_count: 0,
        versions: [capturing],
      },
    ]

    const wrapper = mount(ImagesView, {
      global: {
        stubs: {
          LoadingSpinner: { template: '<div />' },
          Button: { template: '<button><slot /></button>' },
          Card: { template: '<div><slot /></div>' },
          CardContent: { template: '<div><slot /></div>' },
          Badge: { template: '<span><slot /></span>' },
          CreateImageArtifactDialog: { template: '<button>Capture Image</button>' },
          CreateWorkspaceFromImageArtifactDialog: { template: '<div><slot /></div>' },
        },
      },
    })

    expect(wrapper.text()).toContain('Capturing…')
    expect(wrapper.text()).toContain('No ready version')
    expect(wrapper.findAll('button').some((b) => b.text().includes('New workspace'))).toBe(false)
  })
})
