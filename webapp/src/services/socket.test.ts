import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

const { io } = vi.hoisted(() => ({ io: vi.fn() }))
vi.mock('socket.io-client', () => ({ io }))

interface MockSocket {
  connected: boolean
  on: ReturnType<typeof vi.fn>
  off: ReturnType<typeof vi.fn>
  emit: ReturnType<typeof vi.fn>
  connect: ReturnType<typeof vi.fn>
  disconnect: ReturnType<typeof vi.fn>
  trigger: (event: string, data?: unknown) => void
}

function makeSocket(): MockSocket {
  const handlers = new Map<string, Set<(...args: unknown[]) => void>>()
  const mock: MockSocket = {
    connected: false,
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      const set = handlers.get(event) ?? new Set()
      set.add(handler)
      handlers.set(event, set)
      return mock
    }),
    off: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      handlers.get(event)?.delete(handler)
      return mock
    }),
    emit: vi.fn(),
    connect: vi.fn(() => {
      mock.connected = true
      mock.trigger('connect')
    }),
    disconnect: vi.fn(() => {
      mock.connected = false
      mock.trigger('disconnect')
    }),
    trigger: (event: string, data?: unknown) => {
      for (const handler of [...(handlers.get(event) ?? [])]) handler(data)
    },
  }
  return mock
}

let socket: MockSocket
let service: typeof import('./socket')

beforeEach(async () => {
  vi.resetModules()
  vi.clearAllMocks()
  localStorage.clear()
  const { createPinia, setActivePinia } = await import('pinia')
  setActivePinia(createPinia())
  socket = makeSocket()
  io.mockReturnValue(socket)
  service = await import('./socket')
})

afterEach(() => {
  service.disconnect()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  localStorage.clear()
})

describe('browser realtime adapter', () => {
  it('reflects connection state and resumes on tab visibility without retaining disposed listeners', () => {
    const listener = vi.fn()
    service.onEvent('workspace:error', listener)
    service.connect()
    socket.connect()
    expect(service.isConnected.value).toBe(true)
    socket.trigger('workspace:error', {})
    expect(listener).toHaveBeenCalledOnce()
    socket.disconnect()
    expect(service.isConnected.value).toBe(false)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(service.isConnected.value).toBe(true)
    expect(socket.connect).toHaveBeenCalledTimes(2)
    service.disconnect()
    document.dispatchEvent(new Event('visibilitychange'))
    socket.trigger('workspace:error', {})
    expect(service.isConnected.value).toBe(false)
    expect(socket.connect).toHaveBeenCalledTimes(2)
    expect(listener).toHaveBeenCalledOnce()
  })

  it('uses the REST client for auth recovery and reads the latest browser token on reconnect', async () => {
    const { getApiClient } = await import('./api')
    const refresh = vi.spyOn(getApiClient(), 'refreshAccessToken').mockResolvedValue(false)
    service.connect()
    socket.trigger('connect_error', new Error('Unauthorized'))
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledOnce())
    expect(socket.connect).not.toHaveBeenCalled()
    refresh.mockResolvedValue(true)
    socket.trigger('connect_error', new Error('Unauthorized'))
    await vi.waitFor(() => expect(service.isConnected.value).toBe(true))
    localStorage.setItem('kern_access_token', 'refreshed-token')
    const authenticate = io.mock.calls[0]![1].auth
    const accept = vi.fn()
    authenticate(accept)
    expect(accept).toHaveBeenCalledWith({ token: 'refreshed-token' })
  })

  it('forwards terminal and file commands while fencing a transitioning workspace', async () => {
    const { useWorkspaceStore } = await import('@/stores/workspaces')
    service.connect()
    service.sendTerminalInput('ws-1', 'terminal-1', 'aGk=')
    expect(socket.emit).toHaveBeenLastCalledWith('frontend:terminal_input', {
      workspace_id: 'ws-1',
      terminal_id: 'terminal-1',
      data: 'aGk=',
    })
    service.sendFilesUpload('ws-1', 'request-1', '/workspace', 'a.txt', 'aGk=')
    expect(socket.emit.mock.calls.map(([event]) => event)).toEqual([
      'frontend:terminal_input',
      'frontend:files_upload_start',
      'frontend:files_upload_chunk',
      'frontend:files_upload_finish',
    ])
    vi.spyOn(useWorkspaceStore(), 'isWorkspaceTransitioning').mockReturnValue(true)
    socket.emit.mockClear()
    service.sendTerminalInput('ws-1', 'terminal-1', 'aGk=')
    service.sendFilesUpload('ws-1', 'request-2', '/workspace', 'a.txt', 'aGk=')
    expect(service.sendFilesFind('ws-1', 'request-3', 'a', 50)).toBe(false)
    await expect(service.readWorkspaceFile('ws-1', '/workspace/a.png')).rejects.toMatchObject({
      code: 'workspace_transitioning',
    })
    await expect(service.downloadWorkspaceFile('ws-1', '/workspace')).rejects.toMatchObject({
      code: 'workspace_transitioning',
    })
    expect(socket.emit).not.toHaveBeenCalled()
  })

  it('loads chat media through the shared client without view-level file event listeners', async () => {
    const { useWorkspaceImageStore } = await import('@/stores/workspaceImages')
    service.connect()
    socket.connect()
    const store = useWorkspaceImageStore()
    const path = '/workspace/shot.png'
    store.fetchImage('ws-1', path)
    const request = socket.emit.mock.calls.find(([event]) => event === 'frontend:files_read')![1]
    const identity = { workspace_id: 'ws-1', request_id: request.request_id, path }
    socket.trigger('files:content_chunk', {
      ...identity,
      index: 1,
      total_chunks: 2,
      content: 'ZGVm',
    })
    socket.trigger('files:content_chunk', {
      ...identity,
      index: 0,
      total_chunks: 2,
      content: 'YWJj',
    })
    socket.trigger('files:content_result', {
      ...identity,
      content: '',
      size: 6,
      truncated: false,
      mime_type: 'image/png',
      chunked: true,
      total_chunks: 2,
    })
    await flushPromises()
    expect(store.getImageUrl(path)).toBe('data:image/png;base64,YWJjZGVm')
    expect(store.isFetchingImage(path)).toBe(false)

    store.fetchImage('ws-1', '/workspace/pending.png')
    store.reset()
    await flushPromises()
    expect(store.isFetchingImage('/workspace/pending.png')).toBe(false)
    expect(socket.emit).toHaveBeenLastCalledWith('frontend:unsubscribe_workspace', {
      workspace_id: 'ws-1',
    })
  })

  it('loads explorer previews and downloads without view-level file event listeners', async () => {
    const { useFileExplorerStore } = await import('@/stores/fileExplorer')
    service.connect()
    socket.connect()
    const store = useFileExplorerStore()
    const path = '/workspace/a.txt'
    store.selectFile(path, 'ws-1')
    const read = socket.emit.mock.calls.find(([event]) => event === 'frontend:files_read')![1]
    const readIdentity = { workspace_id: 'ws-1', request_id: read.request_id, path }
    socket.trigger('files:content_chunk', {
      ...readIdentity,
      index: 0,
      total_chunks: 1,
      content: 'YWJj',
    })
    socket.trigger('files:content_result', {
      ...readIdentity,
      content: '',
      size: 10_000_000,
      truncated: true,
      chunked: true,
      total_chunks: 1,
      mime_type: 'text/plain',
    })
    await flushPromises()
    expect(store.viewingFile).toMatchObject({ content: 'abc', size: 10_000_000, truncated: true })
    expect(store.isLoadingContent).toBe(false)

    vi.useFakeTimers()
    const save = vi.fn(() => 'blob:download')
    vi.stubGlobal(
      'URL',
      class extends URL {
        static createObjectURL = save
        static revokeObjectURL = vi.fn()
      },
    )
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const pending = store.downloadFile('ws-1', path)
    const download = socket.emit.mock.calls.find(
      ([event]) => event === 'frontend:files_download',
    )![1]
    const downloadIdentity = { workspace_id: 'ws-1', request_id: download.request_id, path }
    socket.trigger('files:download_chunk', {
      ...downloadIdentity,
      index: 0,
      total_chunks: 1,
      content: 'YWJj',
    })
    socket.trigger('files:download_result', {
      ...downloadIdentity,
      content: '',
      filename: 'a.txt',
      is_archive: false,
      size: 3,
      chunked: true,
      total_chunks: 1,
    })
    await pending
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ size: 3 }))
    expect(click).toHaveBeenCalledOnce()
    expect(socket.emit).toHaveBeenLastCalledWith('frontend:unsubscribe_workspace', {
      workspace_id: 'ws-1',
    })
    vi.advanceTimersByTime(1000)
  })
})
