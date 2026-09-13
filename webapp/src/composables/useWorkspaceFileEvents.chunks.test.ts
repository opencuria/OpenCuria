import { defineComponent, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceFileEvents } from './useWorkspaceFileEvents'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { sendFilesRead } from '@/services/socket'

const listeners: Partial<Record<string, (data: never) => void>> = {}

vi.mock('@/services/socket', () => ({
  onEvent: vi.fn((event: string, callback: (data: never) => void) => {
    listeners[event] = callback
    return () => {
      delete listeners[event]
    }
  }),
  sendFilesList: vi.fn(),
  sendFilesFind: vi.fn(),
  sendFilesRead: vi.fn(),
  sendFilesDownload: vi.fn(),
}))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const FileEventsHost = defineComponent({
  props: {
    workspaceId: { type: String, required: true },
  },
  setup(props) {
    const workspaceId = ref(props.workspaceId)
    useWorkspaceFileEvents(workspaceId)
    return { workspaceId }
  },
  template: '<div />',
})

function emit(event: string, data: unknown): void {
  listeners[event]?.(data as never)
}

describe('useWorkspaceFileEvents chunk forwarding', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    for (const key of Object.keys(listeners)) {
      delete listeners[key]
    }
  })

  it('routes files:content_chunk into both the explorer and image stores', () => {
    const explorer = useFileExplorerStore()
    const images = useWorkspaceImageStore()
    const explorerSpy = vi.spyOn(explorer, 'handleContentChunk')
    const imageSpy = vi.spyOn(images, 'handleContentChunk')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emit('files:content_chunk', {
      workspace_id: 'ws-1',
      request_id: 'req-1',
      path: '/workspace/a.txt',
      index: 0,
      total_chunks: 2,
      content: 'QUJD',
    })

    expect(explorerSpy).toHaveBeenCalledWith('req-1', '/workspace/a.txt', 0, 2, 'QUJD')
    expect(imageSpy).toHaveBeenCalledWith('req-1', '/workspace/a.txt', 0, 2, 'QUJD')
  })

  it('routes files:download_chunk into the explorer store', () => {
    const explorer = useFileExplorerStore()
    const spy = vi.spyOn(explorer, 'handleDownloadChunk')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emit('files:download_chunk', {
      workspace_id: 'ws-1',
      request_id: 'req-dl',
      path: '/workspace/a.txt',
      index: 1,
      total_chunks: 2,
      content: 'REVG',
    })

    expect(spy).toHaveBeenCalledWith('req-dl', '/workspace/a.txt', 1, 2, 'REVG')
  })

  it('ignores chunk events for a different workspace', () => {
    const explorer = useFileExplorerStore()
    const spy = vi.spyOn(explorer, 'handleContentChunk')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    emit('files:content_chunk', {
      workspace_id: 'ws-other',
      request_id: 'req-x',
      path: '/workspace/a.txt',
      index: 0,
      total_chunks: 1,
      content: 'QUJD',
    })

    expect(spy).not.toHaveBeenCalled()
  })

  it('drives a full chunked read through socket events into viewer state', () => {
    const explorer = useFileExplorerStore()
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    explorer.selectFile('/workspace/a.txt', 'ws-1')
    const req = vi.mocked(sendFilesRead).mock.calls[0]![1]

    const original = 'routed through the socket listener'
    const full = btoa(original)
    const per = Math.floor(full.length / 2 / 4) * 4
    const p0 = full.slice(0, per)
    const p1 = full.slice(per)

    emit('files:content_chunk', {
      workspace_id: 'ws-1',
      request_id: req,
      path: '/workspace/a.txt',
      index: 1,
      total_chunks: 2,
      content: p1,
    })
    emit('files:content_chunk', {
      workspace_id: 'ws-1',
      request_id: req,
      path: '/workspace/a.txt',
      index: 0,
      total_chunks: 2,
      content: p0,
    })
    emit('files:content_result', {
      workspace_id: 'ws-1',
      request_id: req,
      path: '/workspace/a.txt',
      content: '',
      size: original.length,
      truncated: false,
      chunked: true,
      total_chunks: 2,
    })

    expect(explorer.viewingFile?.content).toBe(original)
    expect(explorer.isLoadingContent).toBe(false)
  })
})
