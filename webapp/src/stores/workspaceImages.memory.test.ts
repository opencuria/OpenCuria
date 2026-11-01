import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'

import { useWorkspaceImageStore } from './workspaceImages'
import HarnessMentionImages from '@/components/chat/HarnessMentionImages.vue'
import { readWorkspaceFile } from '@/services/socket'

vi.mock('@/services/socket', () => ({ readWorkspaceFile: vi.fn() }))

const IMAGE_A = '/workspace/a.png'
const IMAGE_B = '/workspace/b.png'
const IMAGE_C = '/workspace/c.png'
const VIDEO = '/workspace/clip.mp4'

/** Populate the cache through the same read completion used by chat rendering. */
async function load(path: string, bytes: number): Promise<void> {
  vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
    workspace_id: 'ws-1',
    request_id: 'read-1',
    path,
    content: Buffer.alloc(bytes, 1).toString('base64'),
    size: bytes,
    truncated: false,
    mime_type: path === VIDEO ? 'video/mp4' : 'image/png',
  })
  const store = useWorkspaceImageStore()
  if (path === VIDEO) store.fetchVideo('ws-1', path)
  else store.fetchImage('ws-1', path)
  await flushPromises()
}

describe('workspace media memory cache', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.resetAllMocks()
    vi.stubGlobal('atob', (value: string) => Buffer.from(value, 'base64').toString('binary'))
    Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock-video'),
      revokeObjectURL: vi.fn(),
    })
  })

  afterEach(() => {
    useWorkspaceImageStore().reset()
    vi.unstubAllGlobals()
  })

  it('evicts the least recently used unpinned image when reads exceed the budget', async () => {
    const store = useWorkspaceImageStore()
    await load(IMAGE_A, 5 * 1024 * 1024)
    await load(IMAGE_B, 5 * 1024 * 1024)
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull() // Make A more recent.
    await load(IMAGE_C, 5 * 1024 * 1024)

    expect(store.getImageUrl(IMAGE_B)).toBeNull()
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull()
    expect(store.getImageUrl(IMAGE_C)).not.toBeNull()
  })

  it('keeps multiple visible pins alive and trims after the final release', async () => {
    const store = useWorkspaceImageStore()
    store.pinMedia(IMAGE_A, true)
    store.pinMedia(IMAGE_A, true)
    store.pinMedia(IMAGE_B, true)
    store.pinMedia(IMAGE_C, true)
    await load(IMAGE_A, 5 * 1024 * 1024)
    await load(IMAGE_B, 5 * 1024 * 1024)
    await load(IMAGE_C, 5 * 1024 * 1024)

    store.pinMedia(IMAGE_A, false)
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull()
    store.pinMedia(IMAGE_A, false)
    expect(store.getImageUrl(IMAGE_A)).toBeNull()
    expect(store.getImageUrl(IMAGE_B)).not.toBeNull()
    store.releaseMedia([IMAGE_B, IMAGE_C])
    expect(store.getImageUrl(IMAGE_B)).not.toBeNull()
  })

  it('keeps mounted mention media pinned after reset and refetch, then releases on unmount', async () => {
    const store = useWorkspaceImageStore()
    // The component starts the read and owns the pin, including across reset.
    vi.mocked(readWorkspaceFile).mockImplementation(async (_, path) => ({
      workspace_id: 'ws-1', request_id: 'read-1', path,
      content: Buffer.alloc(5 * 1024 * 1024, 1).toString('base64'),
      size: 5 * 1024 * 1024, truncated: false, mime_type: 'image/png',
    }))
    const wrapper = mount(HarnessMentionImages, {
      props: {
        tokens: [IMAGE_A, IMAGE_B, IMAGE_C].map((path, index) => ({
          kind: 'file', path, start: index * 20, end: (index + 1) * 20,
        })),
        workspaceId: 'ws-1',
      },
      global: { stubs: { teleport: true } },
    })
    await flushPromises()
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull()
    store.reset()
    expect(store.getImageUrl(IMAGE_A)).toBeNull()
    await flushPromises()
    expect(store.getImageUrl(IMAGE_A)).not.toBeNull()
    expect(readWorkspaceFile).toHaveBeenCalledTimes(6)
    wrapper.unmount()
    expect([IMAGE_A, IMAGE_B, IMAGE_C].filter((path) => store.getImageUrl(path))).toHaveLength(2)
  })

  it('retains pinned video blobs and revokes them when released or evicted', async () => {
    const store = useWorkspaceImageStore()
    store.pinMedia(VIDEO, true)
    await load(VIDEO, 33 * 1024 * 1024)
    expect(store.getVideoUrl(VIDEO)).toBe('blob:mock-video')
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
    store.pinMedia(VIDEO, false)
    expect(store.getVideoUrl(VIDEO)).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)

    await load(VIDEO, 20 * 1024 * 1024)
    await load(IMAGE_A, 5 * 1024 * 1024)
    expect(store.getVideoUrl(VIDEO)).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  })
})
