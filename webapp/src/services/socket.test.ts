import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
    vi.spyOn(useWorkspaceStore(), 'isWorkspaceTransitioning').mockReturnValue(true)
    socket.emit.mockClear()
    service.sendTerminalInput('ws-1', 'terminal-1', 'aGk=')
    await expect(service.findWorkspaceFiles('ws-1', 'a', 50)).rejects.toMatchObject({
      code: 'workspace_transitioning',
    })
    await expect(service.listWorkspaceFiles('ws-1', '/workspace')).rejects.toMatchObject({
      code: 'workspace_transitioning',
    })
    expect(socket.emit).not.toHaveBeenCalled()
  })

  it('loads directory trees and file suggestions without view listeners and cancels on reset', async () => {
    const { useFileExplorerStore } = await import('@/stores/fileExplorer')
    service.connect()
    socket.connect()
    const store = useFileExplorerStore()
    const listing = store.fetchDirectory('ws-1', '/workspace')
    const request = socket.emit.mock.calls.find(([event]) => event === 'frontend:files_list')![1]
    const entries = [{ name: 'a.txt', path: '/workspace/a.txt', type: 'file', size: 3 }]
    socket.trigger('files:list_result', { ...request, entries })
    await listing
    expect(store.tree).toMatchObject(entries)

    const search = store.findFiles('ws-1', 'a', 10)
    const find = socket.emit.mock.calls.find(([event]) => event === 'frontend:files_find')![1]
    expect(find.limit).toBe(10)
    socket.trigger('files:find_result', {
      ...find,
      paths: [{ name: 'a.txt', path: '/workspace/a.txt' }],
      truncated: false,
    })
    await expect(search).resolves.toEqual(['/workspace/a.txt'])

    const pending = store.fetchDirectory('ws-1', '/workspace')
    const late = socket.emit.mock.calls
      .filter(([event]) => event === 'frontend:files_list')
      .slice(-1)[0]![1]
    const pendingSearch = store.findFiles('ws-1', 'a')
    store.reset()
    await pending
    await expect(pendingSearch).resolves.toEqual([])
    socket.trigger('files:list_result', { ...late, entries })
    expect(store.tree).toEqual([])
    expect(store.loadingPaths.size).toBe(0)
    expect(socket.emit).toHaveBeenLastCalledWith('frontend:unsubscribe_workspace', {
      workspace_id: 'ws-1',
    })
  })
})
