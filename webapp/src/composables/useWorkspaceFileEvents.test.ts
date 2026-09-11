import { defineComponent, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceFileEvents } from './useWorkspaceFileEvents'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'

const listeners: Partial<Record<string, (data: unknown) => void>> = {}

vi.mock('@/services/socket', () => ({
  onEvent: vi.fn((event: string, callback: (data: unknown) => void) => {
    listeners[event] = callback
    return () => {
      delete listeners[event]
    }
  }),
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

    listeners['files:list_result']?.({
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
    )
  })

  it('ignores file events for a different workspace', () => {
    const fileExplorerStore = useFileExplorerStore()
    const spy = vi.spyOn(fileExplorerStore, 'handleListResult')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    listeners['files:list_result']?.({
      workspace_id: 'ws-other',
      request_id: 'req-2',
      path: '/workspace',
      entries: [],
    })

    expect(spy).not.toHaveBeenCalled()
  })

  it('forwards matching files:content_result events to the image store', () => {
    const imageStore = useWorkspaceImageStore()
    const spy = vi.spyOn(imageStore, 'handleContentResult')
    mount(FileEventsHost, { props: { workspaceId: 'ws-1' } })

    listeners['files:content_result']?.({
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
    )
  })
})
