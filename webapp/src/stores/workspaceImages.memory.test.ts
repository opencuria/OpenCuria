import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'

import { useWorkspaceImageStore, MEDIA_CACHE_BUDGET_BYTES } from './workspaceImages'
import HarnessMentionImages, {
  type MentionFileToken,
} from '@/components/chat/HarnessMentionImages.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { readWorkspaceFile } from '@/services/socket'

vi.mock('@/services/socket', () => ({ readWorkspaceFile: vi.fn(() => new Promise(() => {})) }))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const IMAGE_A = '/workspace/a.png'
const IMAGE_B = '/workspace/b.png'
const IMAGE_C = '/workspace/c.png'
const VIDEO = '/workspace/clip.mp4'

function dataUrl(bytes: number): string {
  const payload = Buffer.alloc(bytes, 1).toString('base64')
  return `data:image/png;base64,${payload}`
}

function token(path: string, start = 0): MentionFileToken {
  return { kind: 'file', path, start, end: start + 20 }
}

function mountStrip(tokens: MentionFileToken[]) {
  return mount(HarnessMentionImages, {
    props: { tokens, workspaceId: 'ws-1' },
    attachTo: document.body,
    global: {
      provide: { [harnessWorkspaceIdKey as symbol]: ref('ws-1') },
      stubs: { teleport: true },
    },
  })
}

describe('workspace media memory cache', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock-video'),
      revokeObjectURL: vi.fn(),
    })
    vi.stubGlobal('atob', (value: string) => Buffer.from(value, 'base64').toString('binary'))
  })

  afterEach(() => {
    try {
      useWorkspaceImageStore().reset()
    } catch {
      // Pinia may already be torn down.
    }
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('evicts least-recently-used unpinned image entries over its byte budget', () => {
    const store = useWorkspaceImageStore()
    store.storeLocalImage(IMAGE_A, dataUrl(5 * 1024 * 1024))
    store.storeLocalImage(IMAGE_B, dataUrl(5 * 1024 * 1024))
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull() // Make A more recent.
    store.storeLocalImage(IMAGE_C, dataUrl(5 * 1024 * 1024))

    expect(store.getImageUrl(IMAGE_B)).toBeNull()
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull()
    expect(store.getImageUrl(IMAGE_C)).not.toBeNull()
    expect(MEDIA_CACHE_BUDGET_BYTES).toBe(32 * 1024 * 1024)
  })

  it('keeps multiple visible pins alive and trims after the final release', () => {
    const store = useWorkspaceImageStore()
    store.pinMedia(IMAGE_A, true)
    store.storeLocalImage(IMAGE_A, dataUrl(20 * 1024 * 1024))
    store.pinMedia(IMAGE_A, true)
    store.pinMedia(IMAGE_B, true)
    store.storeLocalImage(IMAGE_B, dataUrl(20 * 1024 * 1024))

    expect(store.imageCache[IMAGE_A]).toBeDefined()
    expect(store.imageCache[IMAGE_B]).toBeDefined()
    store.pinMedia(IMAGE_A, false)
    expect(store.imageCache[IMAGE_A]).toBeDefined()
    store.pinMedia(IMAGE_A, false)
    expect(store.imageCache[IMAGE_A]).toBeUndefined()
    expect(store.imageCache[IMAGE_B]).toBeDefined()
    store.pinMedia(IMAGE_B, false)
  })

  it('batches many releases into a single cache reconciliation and trim', () => {
    const store = useWorkspaceImageStore()
    const paths = Array.from({ length: 17 }, (_, index) => `/workspace/batch-${index}.png`)
    const payload = `data:image/png;base64,${'x'.repeat(1_000_000)}`
    for (const path of paths) {
      store.pinMedia(path, true)
      store.storeLocalImage(path, payload)
    }

    const keysSpy = vi.spyOn(Object, 'keys')
    store.releaseMedia(paths)
    const imageCacheEnumerations = keysSpy.mock.calls.filter(
      ([object]) => object === store.imageCache,
    ).length
    keysSpy.mockRestore()

    expect(imageCacheEnumerations).toBe(1)
    expect(Object.keys(store.imageCache).length).toBeLessThan(paths.length)
  })

  it('drops unpinned oversized images but preserves pinned oversized media', () => {
    const store = useWorkspaceImageStore()
    store.storeLocalImage(IMAGE_A, dataUrl(20 * 1024 * 1024))
    expect(store.getImageUrl(IMAGE_A)).toBeNull()

    store.pinMedia(IMAGE_B, true)
    store.storeLocalImage(IMAGE_B, dataUrl(20 * 1024 * 1024))
    expect(store.imageCache[IMAGE_B]).toBeDefined()
    store.pinMedia(IMAGE_B, false)
    expect(store.getImageUrl(IMAGE_B)).toBeNull()
  })

  it('preserves cached preview while mention component uses it and releases on unmount', async () => {
    const store = useWorkspaceImageStore()
    store.pinMedia(IMAGE_A, true)
    store.storeLocalImage(IMAGE_A, dataUrl(20 * 1024 * 1024))
    const wrapper = mountStrip([token(IMAGE_A)])
    store.pinMedia(IMAGE_A, false)
    await wrapper.vm.$nextTick()
    store.storeLocalImage(IMAGE_B, dataUrl(20 * 1024 * 1024))

    expect(store.imageCache[IMAGE_A]).toBeDefined()
    wrapper.unmount()
    expect(store.getImageUrl(IMAGE_A)).toBeNull()
  })

  it('keeps mounted mention media pinned after a cache reset and refetch', async () => {
    const store = useWorkspaceImageStore()
    store.pinMedia(IMAGE_A, true)
    store.storeLocalImage(IMAGE_A, dataUrl(20 * 1024 * 1024))
    const wrapper = mountStrip([token(IMAGE_A)])
    store.pinMedia(IMAGE_A, false)
    await wrapper.vm.$nextTick()
    const payload = Buffer.alloc(20 * 1024 * 1024, 1).toString('base64')
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
      workspace_id: 'ws-1',
      request_id: 'read-1',
      path: IMAGE_A,
      content: payload,
      size: 20 * 1024 * 1024,
      truncated: false,
      mime_type: 'image/png',
    })
    store.reset()
    expect(store.imageCache[IMAGE_A]).toBeUndefined()
    await flushPromises()
    expect(store.imageCache[IMAGE_A]).toBeDefined()

    store.storeLocalImage(IMAGE_B, dataUrl(20 * 1024 * 1024))
    expect(store.imageCache[IMAGE_A]).toBeDefined()
    wrapper.unmount()
    expect(store.getImageUrl(IMAGE_A)).toBeNull()
  })

  it('does not revoke a video while pinned and revokes it after release', async () => {
    const store = useWorkspaceImageStore()
    const payload = Buffer.alloc(40 * 1024 * 1024, 1).toString('base64')
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
      workspace_id: 'ws-1',
      request_id: 'read-1',
      path: VIDEO,
      content: payload,
      size: Buffer.from(payload, 'base64').length,
      truncated: false,
      mime_type: 'video/mp4',
    })
    store.fetchVideo('ws-1', VIDEO)
    store.pinMedia(VIDEO, true)
    await flushPromises()

    expect(store.videoCache[VIDEO]).toBe('blob:mock-video')
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
    store.pinMedia(VIDEO, false)
    expect(store.videoCache[VIDEO]).toBeUndefined()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-video')
  })

  it('revokes videos when evicted from cache', async () => {
    const store = useWorkspaceImageStore()
    const payload = Buffer.alloc(20 * 1024 * 1024, 1).toString('base64')
    store.pinMedia(VIDEO, true)
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
      workspace_id: 'ws-1',
      request_id: 'read-1',
      path: VIDEO,
      content: payload,
      size: Buffer.from(payload, 'base64').length,
      truncated: false,
      mime_type: 'video/mp4',
    })
    store.fetchVideo('ws-1', VIDEO)
    await flushPromises()
    store.pinMedia(VIDEO, false)
    store.storeLocalImage(IMAGE_A, dataUrl(20 * 1024 * 1024))

    expect(store.getVideoUrl(VIDEO)).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-video')
  })

  it('drops optimistic upload preview on upload failure, including when pinned', () => {
    const store = useWorkspaceImageStore()
    store.trackUpload('upload-1', IMAGE_A)
    store.storeLocalImage(IMAGE_A, dataUrl(1))
    store.pinMedia(IMAGE_A, true)
    store.handleUploadResult('upload-1', 'error', 'failed')
    expect(store.imageCache[IMAGE_A]).toBeDefined()
    store.pinMedia(IMAGE_A, false)
    expect(store.imageCache[IMAGE_A]).toBeUndefined()
  })
})
