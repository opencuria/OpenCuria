import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import CapturedImagesPanel from './CapturedImagesPanel.vue'
import type { ImageArtifact } from '@/types'

const deleteImageArtifact = vi.fn(async () => undefined)
const fetchImages = vi.fn(async () => undefined)
const renameImageArtifact = vi.fn(async () => undefined)

const captured: ImageArtifact = {
  id: 'img-1',
  name: 'Before refactor',
  artifact_kind: 'captured',
  status: 'ready',
  runtime_type: 'qemu',
  size_bytes: 1024,
  created_at: '2026-01-01T00:00:00.000Z',
  runner_artifact_id: 'art-1',
  created_by_id: 1,
  is_deactivated: false,
  source_runner_online: true,
  source_definition_name: null,
  source_workspace_id: 'ws-12345678',
}

vi.mock('@/stores/images', () => ({
  useImageStore: () => ({
    images: [captured],
    loading: false,
    error: null,
    fetchImages,
    deleteImageArtifact,
    renameImageArtifact,
  }),
}))

vi.mock('@/composables/usePolling', () => ({
  usePolling: () => ({ start: vi.fn() }),
}))

describe('CapturedImagesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('asks for confirmation in a dialog instead of window.confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    const wrapper = mount(CapturedImagesPanel, {
      attachTo: document.body,
      global: {
        stubs: {
          CreateImageArtifactDialog: { template: '<div />' },
          CreateWorkspaceFromImageArtifactDialog: { template: '<div />' },
          Dialog: { template: '<div><slot /></div>' },
          DialogContent: { template: '<div><slot /></div>' },
          DialogHeader: { template: '<div><slot /></div>' },
          DialogTitle: { template: '<div><slot /></div>' },
          DialogDescription: { template: '<div><slot /></div>' },
          DialogFooter: { template: '<div><slot /></div>' },
        },
      },
    })
    await flushPromises()

    try {
      expect(wrapper.text()).toContain('Before refactor')
      await wrapper.get('button[title="Delete image"]').trigger('click')
      await flushPromises()

      expect(confirmSpy).not.toHaveBeenCalled()
      expect(wrapper.text()).toContain('Delete Before refactor')
      expect(deleteImageArtifact).not.toHaveBeenCalled()

      const deleteBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
      expect(deleteBtn).toBeTruthy()
      await deleteBtn!.trigger('click')
      await flushPromises()

      expect(deleteImageArtifact).toHaveBeenCalledWith('img-1')
    } finally {
      wrapper.unmount()
      confirmSpy.mockRestore()
    }
  })
})
