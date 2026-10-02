import { defineComponent, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceFileEvents } from './useWorkspaceFileEvents'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { useNotificationStore } from '@/stores/notifications'
import { CHAT_UPLOAD_DIR } from '@/lib/chatUpload'
import { sendFilesList } from '@/services/socket'

const listeners: Partial<Record<string, Array<(data: unknown) => void>>> = {}

function emitEvent(event: string, data: unknown): void {
  for (const listener of listeners[event] ?? []) listener(data)
}

vi.mock('@/services/socket', () => ({
  sendFilesList: vi.fn(),
  onEvent: vi.fn((event: string, callback: (data: unknown) => void) => {
    listeners[event] ??= []
    listeners[event]!.push(callback)
    return () => {
      listeners[event] = listeners[event]?.filter((listener) => listener !== callback)
      if (listeners[event]?.length === 0) delete listeners[event]
    }
  }),
}))

const FileEventsHost = defineComponent({
  props: {
    workspaceId: { type: String, required: true },
  },
  setup(props) {
    const currentWorkspaceId = ref(props.workspaceId)
    useWorkspaceFileEvents(currentWorkspaceId)
    return { currentWorkspaceId }
  },
  template: '<div />',
})

describe('useWorkspaceFileEvents', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    for (const key of Object.keys(listeners)) {
      delete listeners[key]
    }
  })

  it('routes matching files:list_result events to the file explorer store', () => {
    const fileExplorerStore = useFileExplorerStore()
    const spy = vi.spyOn(fileExplorerStore, 'handleListResult')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emitEvent('files:list_result', {
      workspace_id: 'ws-1',
      request_id: 'req-1',
      path: '/workspace',
      entries: [{ name: 'README.md', path: '/workspace/README.md', type: 'file', size: 12 }],
    })

    expect(spy).toHaveBeenCalledWith(
      'req-1',
      '/workspace',
      [{ name: 'README.md', path: '/workspace/README.md', type: 'file', size: 12 }],
      undefined,
      'ws-1',
      true,
    )
  })

  it('ignores file events for a different workspace', () => {
    const fileExplorerStore = useFileExplorerStore()
    const spy = vi.spyOn(fileExplorerStore, 'handleListResult')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emitEvent('files:list_result', {
      workspace_id: 'ws-other',
      request_id: 'req-2',
      path: '/workspace',
      entries: [],
    })

    expect(spy).not.toHaveBeenCalled()
  })

  it('does not refresh the shared explorer after an isolated scheduled upload', async () => {
    const store = useFileExplorerStore()
    const entries = [
      { name: '.opencuria', path: '/workspace/.opencuria', type: 'directory' as const, size: 0 },
    ]
    vi.spyOn(store, 'fetchDirectoryEntries').mockResolvedValue(entries)
    const upload = store.trackAndUpload(
      'ws-1',
      'upload-1',
      CHAT_UPLOAD_DIR,
      'one.txt',
      'aGVsbG8=',
      false,
    )
    const refresh = vi.spyOn(store, 'fetchDirectory')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emitEvent('files:upload_result', {
      workspace_id: 'ws-1',
      request_id: 'upload-1',
      path: CHAT_UPLOAD_DIR,
      status: 'success',
    })
    await upload

    expect(refresh).not.toHaveBeenCalled()
  })

  it('handles a broadcast upload result once across duplicate workspace listeners', async () => {
    const notify = useNotificationStore()
    const notificationSpy = vi.spyOn(notify, 'error')
    const fileExplorer = useFileExplorerStore()
    const images = useWorkspaceImageStore()
    vi.spyOn(fileExplorer, 'fetchDirectory').mockResolvedValue(undefined)
    const imageResult = vi.spyOn(images, 'handleUploadResult')
    const uploadHandler = vi.spyOn(fileExplorer, 'handleUploadResult')
    const upload = fileExplorer.trackAndUpload(
      'ws-1',
      'duplicate-success',
      '/workspace',
      'shared.txt',
      'eA==',
    )
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emitEvent('files:upload_result', {
      workspace_id: 'ws-1',
      request_id: 'duplicate-success',
      path: '/workspace',
      status: 'success',
    })
    await upload
    emitEvent('files:upload_result', {
      workspace_id: 'ws-1',
      request_id: 'duplicate-success',
      path: '/workspace',
      status: 'success',
    })

    expect(uploadHandler).toHaveBeenCalledTimes(4)
    expect(uploadHandler.mock.results.map((result) => result.value)).toEqual([
      true,
      false,
      false,
      false,
    ])
    expect(vi.mocked(sendFilesList)).toHaveBeenCalledTimes(1)
    expect(imageResult).toHaveBeenCalledTimes(4)
    expect(images.getUploadStatus('/workspace/shared.txt')).toBeNull()
    expect(notificationSpy).not.toHaveBeenCalled()
  })

  it('routes image-only uploads despite duplicate explorer listeners', () => {
    const fileExplorer = useFileExplorerStore()
    const uploadHandler = vi.spyOn(fileExplorer, 'handleUploadResult')
    const images = useWorkspaceImageStore()
    const imageResult = vi.spyOn(images, 'handleUploadResult')
    images.trackUpload('image-only-upload', '/workspace/photo.png')
    images.storeLocalImage('/workspace/photo.png', 'data:image/png;base64,eA==')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    for (let i = 0; i < 2; i++) {
      emitEvent('files:upload_result', {
        workspace_id: 'ws-1',
        request_id: 'image-only-upload',
        path: '/workspace/photo.png',
        status: 'error',
        error: 'upload failed',
      })
    }

    expect(uploadHandler).toHaveBeenCalledTimes(4)
    expect(uploadHandler.mock.results.map((result) => result.value)).toEqual([
      false,
      false,
      false,
      false,
    ])
    expect(imageResult).toHaveBeenCalledTimes(4)
    expect(images.getUploadStatus('/workspace/photo.png')).toBe('error')
    expect(images.getImageUrl('/workspace/photo.png')).toBeNull()
  })

  it('keeps isolated scheduled uploads quiet with duplicate listeners and late cancelled replies', async () => {
    const notify = useNotificationStore()
    const notificationSpy = vi.spyOn(notify, 'error')
    const fileExplorer = useFileExplorerStore()
    const images = useWorkspaceImageStore()
    const refresh = vi.spyOn(fileExplorer, 'fetchDirectory')
    refresh.mockClear()
    const imageResult = vi.spyOn(images, 'handleUploadResult')
    const upload = fileExplorer.trackAndUpload(
      'ws-1',
      'isolated-duplicate',
      '/workspace/task',
      'task.txt',
      'eA==',
      false,
    )
    const cancelled = fileExplorer.trackAndUpload(
      'ws-1',
      'cancelled-duplicate',
      '/workspace/task',
      'cancel.txt',
      'eA==',
      false,
    )
    const cancelledRejected = expect(cancelled).rejects.toThrow('Upload cancelled.')
    fileExplorer.cancelUpload('cancelled-duplicate')
    await cancelledRejected
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emitEvent('files:upload_result', {
      workspace_id: 'ws-1',
      request_id: 'isolated-duplicate',
      path: '/workspace/task',
      status: 'success',
    })
    await upload
    emitEvent('files:upload_result', {
      workspace_id: 'ws-1',
      request_id: 'isolated-duplicate',
      path: '/workspace/task',
      status: 'success',
    })
    for (let i = 0; i < 2; i++) {
      emitEvent('files:upload_result', {
        workspace_id: 'ws-1',
        request_id: 'cancelled-duplicate',
        path: '/workspace/task',
        status: 'error',
        error: 'late failure',
      })
    }

    expect(refresh).not.toHaveBeenCalled()
    expect(imageResult).toHaveBeenCalledTimes(8)
    expect(images.getUploadStatus('/workspace/task/task.txt')).toBeNull()
    expect(notificationSpy).not.toHaveBeenCalled()
  })

  it('ignores unknown upload results so other clients cannot refresh this explorer', () => {
    const fileExplorer = useFileExplorerStore()
    const refresh = vi.spyOn(fileExplorer, 'fetchDirectory')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })
    emitEvent('files:upload_result', {
      workspace_id: 'ws-1',
      request_id: 'from-another-client',
      path: '/workspace',
      status: 'success',
    })
    expect(refresh).not.toHaveBeenCalled()
  })

  it('forwards matching files:content_result events to the image store', () => {
    const imageStore = useWorkspaceImageStore()
    const spy = vi.spyOn(imageStore, 'handleContentResult')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emitEvent('files:content_result', {
      workspace_id: 'ws-1',
      request_id: 'req-3',
      path: '/workspace/shot.png',
      content: 'abc',
      size: 3,
      truncated: false,
      mime_type: 'image/png',
    })

    expect(spy).toHaveBeenCalledWith(
      'req-3',
      '/workspace/shot.png',
      'abc',
      undefined,
      'image/png',
      { chunked: undefined, totalChunks: undefined },
    )
  })
})
