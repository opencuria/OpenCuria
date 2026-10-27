import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import type { FileContentResult } from '@opencuria/client/types'
import { readWorkspaceFile } from '@/services/socket'
import { useNotificationStore } from './notifications'
import { useWorkspaceImageStore } from './workspaceImages'

vi.mock('@/services/socket', () => ({ readWorkspaceFile: vi.fn() }))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

function deferredRead() {
  let resolve!: (result: FileContentResult) => void
  let reject!: (error: Error) => void
  const promise = new Promise<FileContentResult>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function result(path: string): FileContentResult {
  return {
    workspace_id: 'ws-1',
    request_id: 'read-1',
    path,
    content: btoa('image'),
    size: 5,
    truncated: false,
    mime_type: 'image/webp',
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
})

afterEach(() => useWorkspaceImageStore().reset())

describe('workspace media reads', () => {
  it('deduplicates reads, caches the returned MIME type and uses the video read limit', async () => {
    const pending = deferredRead()
    vi.mocked(readWorkspaceFile).mockReturnValue(pending.promise)
    const store = useWorkspaceImageStore()
    const path = '/workspace/image.png'
    store.fetchImage('ws-1', path)
    store.fetchImage('ws-1', path)
    expect(readWorkspaceFile).toHaveBeenCalledOnce()
    expect(store.isFetchingImage(path)).toBe(true)
    pending.resolve(result(path))
    await flushPromises()
    expect(store.getImageUrl(path)).toBe(`data:image/webp;base64,${btoa('image')}`)
    expect(store.isFetchingImage(path)).toBe(false)
    store.fetchImage('ws-1', path)
    expect(readWorkspaceFile).toHaveBeenCalledOnce()

    vi.mocked(readWorkspaceFile).mockReturnValue(new Promise(() => {}))
    store.fetchVideo('ws-1', '/workspace/video.mp4')
    expect(readWorkspaceFile).toHaveBeenLastCalledWith(
      'ws-1',
      '/workspace/video.mp4',
      100 * 1024 * 1024,
      expect.any(AbortSignal),
    )
  })

  it('bounds concurrency and releases a failed read so the queue and retries progress', async () => {
    const pending = deferredRead()
    vi.mocked(readWorkspaceFile).mockReturnValue(new Promise(() => {}))
    vi.mocked(readWorkspaceFile).mockReturnValueOnce(pending.promise)
    const notify = vi.spyOn(useNotificationStore(), 'error')
    const store = useWorkspaceImageStore()
    for (let index = 0; index < 5; index++) store.fetchImage('ws-1', `/workspace/${index}.png`)
    expect(readWorkspaceFile).toHaveBeenCalledTimes(4)
    pending.reject(new Error('File transfer timed out'))
    await flushPromises()
    expect(readWorkspaceFile).toHaveBeenCalledTimes(5)
    expect(store.isFetchingImage('/workspace/0.png')).toBe(false)
    expect(store.getImageUrl('/workspace/0.png')).toBeNull()
    expect(notify).toHaveBeenCalledWith('Media load failed', 'File transfer timed out')
    store.fetchImage('ws-1', '/workspace/0.png')
    expect(store.isFetchingImage('/workspace/0.png')).toBe(true)
  })

  it('aborts reads on reset and ignores their late completions without releasing new slots', async () => {
    const stale = deferredRead()
    vi.mocked(readWorkspaceFile).mockReturnValueOnce(stale.promise)
    const store = useWorkspaceImageStore()
    store.fetchImage('ws-1', '/workspace/stale.png')
    const signal = vi.mocked(readWorkspaceFile).mock.calls[0]![3]!
    store.reset()
    expect(signal.aborted).toBe(true)

    vi.mocked(readWorkspaceFile).mockReturnValue(new Promise(() => {}))
    for (let index = 0; index < 5; index++) store.fetchImage('ws-2', `/workspace/${index}.png`)
    expect(readWorkspaceFile).toHaveBeenCalledTimes(5)
    stale.resolve(result('/workspace/stale.png'))
    await flushPromises()
    expect(readWorkspaceFile).toHaveBeenCalledTimes(5)
    expect(store.getImageUrl('/workspace/stale.png')).toBeNull()
    expect(store.isFetchingImage('/workspace/4.png')).toBe(true)
  })
})
