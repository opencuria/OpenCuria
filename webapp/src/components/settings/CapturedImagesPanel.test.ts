import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import CapturedImagesPanel from './CapturedImagesPanel.vue'
import type { CapturedImage, ImageArtifact } from '@/types'

const { requestDeletion } = vi.hoisted(() => ({
  requestDeletion: vi.fn(async () => ({
    id: 'request',
    phase: 'waiting_dependency',
    diagnostic: 'Pinned workspace',
    can_cancel: true,
  })),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ isAdmin: false }) }))
vi.mock('@/services/runnerStorage.api', () => ({
  listDeletions: vi.fn(async () => []),
  requestDeletion,
  cancelDeletion: vi.fn(),
  previewDeletion: vi.fn(),
}))

const fetchCapturedImages = vi.fn(async () => undefined)
const renameCapturedImage = vi.fn(async () => true)

function version(patch: Partial<ImageArtifact>): ImageArtifact {
  return {
    id: 'v',
    name: 'Before refactor',
    artifact_kind: 'captured',
    status: 'ready',
    runtime_type: 'qemu',
    size_bytes: 1024,
    created_at: '2026-01-01T00:00:00.000Z',
    runner_artifact_id: 'art',
    created_by_id: 1,
    source_workspace_id: 'ws-12345678',
    captured_image_id: 'line-1',
    ...patch,
  }
}

const image: CapturedImage = {
  id: 'line-1',
  name: 'Before refactor',
  status: 'active',
  runner_id: 'r',
  runner_online: true,
  created_by_id: 1,
  created_at: '2026-01-01T00:00:00.000Z',
  latest_id: 'v2',
  latest_version: 2,
  total_size_bytes: 2048,
  workspace_count: 1,
  versions: [
    version({ id: 'v2', version: 2, is_latest: true, retention: 'latest', message: 'Node 22' }),
    version({
      id: 'v1',
      version: 1,
      retention: 'kept',
      message: 'Initial',
      workspace_count: 1,
      workspaces: [{ id: 'ws', name: 'feature-x', created_by_id: 1 }],
    }),
  ],
}

vi.mock('@/stores/images', () => ({
  useImageStore: () => ({
    capturedImages: [image],
    loading: false,
    error: null,
    fetchCapturedImages,
    renameCapturedImage,
  }),
}))

vi.mock('@/composables/usePolling', () => ({
  usePolling: () => ({ start: vi.fn() }),
}))

function mountPanel() {
  return mount(CapturedImagesPanel, {
    attachTo: document.body,
    global: {
      stubs: {
        CreateImageArtifactDialog: { template: '<div />' },
        CreateWorkspaceFromImageArtifactDialog: {
          props: ['capturedImageId'],
          template: '<div data-testid="new-workspace" :data-image="capturedImageId"><slot /></div>',
        },
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: { template: '<div><slot /></div>' },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        DialogFooter: { template: '<div><slot /></div>' },
        Collapsible: { template: '<div><slot /></div>' },
        CollapsibleTrigger: { template: '<button><slot /></button>' },
        CollapsibleContent: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('CapturedImagesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows one row per image with its versions, usage and latest version', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    try {
      expect(wrapper.findAll('[data-testid="captured-image-row"]')).toHaveLength(1)
      expect(wrapper.text()).toContain('2 versions')
      expect(wrapper.text()).toContain('used by 1 workspace')
      const rows = wrapper.findAll('[data-testid="image-version-row"]')
      expect(rows[0]!.text()).toContain('v2')
      expect(rows[0]!.text()).toContain('Latest')
      expect(rows[1]!.text()).toContain('Kept')
      expect(rows[1]!.text()).toContain('feature-x')
      expect(wrapper.get('[data-testid="new-workspace"]').attributes('data-image')).toBe('line-1')
    } finally {
      wrapper.unmount()
    }
  })

  it('only offers deleting non-latest versions individually', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    try {
      const deletes = wrapper.findAll('[data-testid="image-version-delete"]')
      expect(deletes).toHaveLength(1)
      await deletes[0]!.trigger('click')
      await flushPromises()
      await wrapper
        .findAll('button')
        .find((b) => b.text() === 'Store deferred deletion intent')!
        .trigger('click')
      await flushPromises()
      expect(requestDeletion).toHaveBeenCalledWith(
        { target_type: 'image', target_id: 'v1' },
        'deferred',
        '',
      )
    } finally {
      wrapper.unmount()
    }
  })

  it('deletes the whole image through a confirmation dialog instead of window.confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    const wrapper = mountPanel()
    await flushPromises()

    try {
      await wrapper.get('button[title="Delete image"]').trigger('click')
      await flushPromises()
      expect(confirmSpy).not.toHaveBeenCalled()
      expect(wrapper.text()).toContain('image with all versions')

      const deleteBtn = wrapper
        .findAll('button')
        .find((b) => b.text() === 'Store deferred deletion intent')
      expect(deleteBtn).toBeTruthy()
      await deleteBtn!.trigger('click')
      await flushPromises()

      expect(requestDeletion).toHaveBeenCalledWith(
        { target_type: 'captured_image', target_id: 'line-1' },
        'deferred',
        '',
      )
      expect(wrapper.text()).toContain('waiting_dependency')
      expect(wrapper.text()).toContain('Pinned workspace')
    } finally {
      wrapper.unmount()
      confirmSpy.mockRestore()
    }
  })
})
