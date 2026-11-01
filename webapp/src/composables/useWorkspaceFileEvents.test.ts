import { defineComponent, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceFileEvents } from './useWorkspaceFileEvents'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useNotificationStore } from '@/stores/notifications'
import { CHAT_UPLOAD_DIR } from '@/lib/chatUpload'
import { listWorkspaceFiles } from '@/services/socket'

const listeners: Partial<Record<string, Array<(data: unknown) => void>>> = {}

function emitEvent(event: string, data: unknown): void {
  for (const listener of listeners[event] ?? []) listener(data)
}

vi.mock('@/services/socket', () => ({
  listWorkspaceFiles: vi.fn(async () => ({ entries: [] })),
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
    vi.clearAllMocks()
    for (const key of Object.keys(listeners)) {
      delete listeners[key]
    }
  })

  it('ignores upload results for a different workspace and detaches on unmount', () => {
    const handler = vi.spyOn(useFileExplorerStore(), 'handleUploadResult')
    const host = mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })
    emitEvent('files:upload_result', { workspace_id: 'ws-other', request_id: 'other' })
    expect(handler).not.toHaveBeenCalled()
    host.unmount()
    expect(listeners).toEqual({})
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
    vi.spyOn(fileExplorer, 'fetchDirectory').mockResolvedValue(undefined)
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
    expect(vi.mocked(listWorkspaceFiles)).toHaveBeenCalledTimes(1)
    expect(notificationSpy).not.toHaveBeenCalled()
  })

  it('keeps isolated scheduled uploads quiet with duplicate listeners and late cancelled replies', async () => {
    const notify = useNotificationStore()
    const notificationSpy = vi.spyOn(notify, 'error')
    const fileExplorer = useFileExplorerStore()
    const refresh = vi.spyOn(fileExplorer, 'fetchDirectory')
    refresh.mockClear()
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
})
