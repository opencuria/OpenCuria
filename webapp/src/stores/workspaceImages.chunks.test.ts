import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useWorkspaceImageStore } from './workspaceImages'
import { sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesRead: vi.fn(),
}))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const WS_ID = 'ws-1'
const IMG_PATH = '/workspace/shot.png'
const OTHER_PATH = '/workspace/other.png'
const VIDEO_PATH = '/workspace/clip.mp4'

function lastReadRequestId(): string {
  const calls = vi.mocked(sendFilesRead).mock.calls
  return calls[calls.length - 1]![1]
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useRealTimers()
  Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:mock-video'),
    revokeObjectURL: vi.fn(),
  })
  vi.stubGlobal('atob', (s: string) => Buffer.from(s, 'base64').toString('binary'))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
  try {
    useWorkspaceImageStore().reset()
  } catch {
    // ignore — pinia already torn down
  }
})

describe('workspaceImages chunked media', () => {
  it('clears fetching flags and cache on chunked transfer followed by error final', () => {
    const store = useWorkspaceImageStore()
    store.fetchImage(WS_ID, IMG_PATH)
    const req = lastReadRequestId()
    expect(store.isFetchingImage(IMG_PATH)).toBe(true)

    store.handleContentChunk(req, IMG_PATH, 0, 1, btoa('fake-image-bytes'))
    store.handleContentResult(req, IMG_PATH, '', 'boom')

    expect(store.isFetchingImage(IMG_PATH)).toBe(false)
    expect(store.getImageUrl(IMG_PATH)).toBeNull()
  })

  it('assembles a chunked image read into the cache', () => {
    const store = useWorkspaceImageStore()
    store.fetchImage(WS_ID, IMG_PATH)
    const req = lastReadRequestId()

    const full = btoa('fake-image-bytes')
    store.handleContentChunk(req, IMG_PATH, 0, 1, full)
    store.handleContentResult(req, IMG_PATH, '', undefined, 'image/png', {
      chunked: true,
      totalChunks: 1,
    })

    expect(store.getImageUrl(IMG_PATH)).toBe(`data:image/png;base64,${full}`)
    expect(store.isFetchingImage(IMG_PATH)).toBe(false)
  })

  it('fails the transfer and cleans up on chunk path mismatch', () => {
    const store = useWorkspaceImageStore()
    store.fetchImage(WS_ID, IMG_PATH)
    const req = lastReadRequestId()

    // Chunk claims a different path than the pending read — must fail
    // closed with cleanup instead of silently dropping (stuck spinner).
    store.handleContentChunk(req, OTHER_PATH, 0, 1, btoa('evil-bytes'))

    expect(store.getImageUrl(IMG_PATH)).toBeNull()
    expect(store.getImageUrl(OTHER_PATH)).toBeNull()
    expect(store.isFetchingImage(IMG_PATH)).toBe(false)
  })

  it('clears the video fetching flag when the final errors', () => {
    const store = useWorkspaceImageStore()
    store.fetchVideo(WS_ID, VIDEO_PATH)
    const req = lastReadRequestId()
    expect(store.isFetchingVideo(VIDEO_PATH)).toBe(true)

    store.handleContentResult(req, VIDEO_PATH, '', 'read failed')

    expect(store.isFetchingVideo(VIDEO_PATH)).toBe(false)
    expect(store.getVideoUrl(VIDEO_PATH)).toBeNull()
  })
})

describe('workspaceImages timeouts and assembled errors', () => {
  it('times out a fetchImage with no reply (fake timers, slot released)', () => {
    vi.useFakeTimers()
    try {
      const store = useWorkspaceImageStore()
      store.fetchImage(WS_ID, IMG_PATH)
      expect(store.isFetchingImage(IMG_PATH)).toBe(true)
      vi.advanceTimersByTime(30_001)
      expect(store.isFetchingImage(IMG_PATH)).toBe(false)
      expect(store.getImageUrl(IMG_PATH)).toBeNull()
      // A late result after the timeout must not repopulate.
      const req = lastReadRequestId()
      store.handleContentResult(req, IMG_PATH, btoa('late-bytes'), undefined, 'image/png')
      expect(store.getImageUrl(IMG_PATH)).toBeNull()
      // Slot released: a fresh fetch works.
      store.fetchImage(WS_ID, IMG_PATH)
      expect(store.isFetchingImage(IMG_PATH)).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('times out a fetchVideo with no reply (fake timers)', () => {
    vi.useFakeTimers()
    try {
      const store = useWorkspaceImageStore()
      store.fetchVideo(WS_ID, VIDEO_PATH)
      expect(store.isFetchingVideo(VIDEO_PATH)).toBe(true)
      vi.advanceTimersByTime(30_001)
      expect(store.isFetchingVideo(VIDEO_PATH)).toBe(false)
      expect(store.getVideoUrl(VIDEO_PATH)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not double-release the slot when timeout and late result both run', () => {
    vi.useFakeTimers()
    try {
      const store = useWorkspaceImageStore()
      store.fetchVideo(WS_ID, VIDEO_PATH)
      const req = lastReadRequestId()
      vi.advanceTimersByTime(30_001)
      expect(store.isFetchingVideo(VIDEO_PATH)).toBe(false)
      // Late chunked traffic for the timed-out request is ignored.
      store.handleContentChunk(req, VIDEO_PATH, 0, 1, btoa('late'))
      store.handleContentResult(req, VIDEO_PATH, '', undefined, 'video/mp4', {
        chunked: true,
        totalChunks: 1,
      })
      expect(store.getVideoUrl(VIDEO_PATH)).toBeNull()
      // Exactly one slot was released: one fresh fetch occupies it.
      store.fetchVideo(WS_ID, VIDEO_PATH)
      expect(store.isFetchingVideo(VIDEO_PATH)).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('handles malformed base64 video payload without throwing or leaking the slot', () => {
    const store = useWorkspaceImageStore()
    store.fetchVideo(WS_ID, VIDEO_PATH)
    const req = lastReadRequestId()
    vi.stubGlobal('atob', () => {
      throw new Error('Invalid character')
    })
    try {
      expect(() =>
        store.handleContentResult(req, VIDEO_PATH, '!!!not-base64!!!', undefined, 'video/mp4'),
      ).not.toThrow()
    } finally {
      vi.unstubAllGlobals()
      vi.stubGlobal('atob', (s: string) => Buffer.from(s, 'base64').toString('binary'))
    }
    expect(store.getVideoUrl(VIDEO_PATH)).toBeNull()
    expect(store.isFetchingVideo(VIDEO_PATH)).toBe(false)
    // Slot released: next fetch proceeds.
    store.fetchVideo(WS_ID, VIDEO_PATH)
    expect(store.isFetchingVideo(VIDEO_PATH)).toBe(true)
  })

  it('fails closed when the chunked final path mismatches the request', () => {
    const store = useWorkspaceImageStore()
    store.fetchImage(WS_ID, IMG_PATH)
    const req = lastReadRequestId()
    const full = btoa('fake-image-bytes')
    store.handleContentChunk(req, IMG_PATH, 0, 1, full)
    store.handleContentResult(req, OTHER_PATH, '', undefined, 'image/png', {
      chunked: true,
      totalChunks: 1,
    })
    expect(store.getImageUrl(IMG_PATH)).toBeNull()
    expect(store.isFetchingImage(IMG_PATH)).toBe(false)
  })
})
