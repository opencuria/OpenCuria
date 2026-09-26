import { beforeEach, describe, expect, it, vi } from 'vitest'

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
  socket = makeSocket()
  io.mockReturnValue(socket)
  service = await import('./socket')
})

describe('socket workspace subscriptions and event listeners', () => {
  it('subscribes once per workspace while multiple consumers hold a reference', () => {
    service.connect()
    socket.connect()
    socket.emit.mockClear()

    const releaseFirst = service.subscribeToWorkspace('ws-1')
    const releaseSecond = service.subscribeToWorkspace('ws-1')
    expect(socket.emit).toHaveBeenCalledTimes(1)
    expect(socket.emit).toHaveBeenLastCalledWith('frontend:subscribe_workspace', { workspace_id: 'ws-1' })

    releaseFirst()
    expect(socket.emit).toHaveBeenCalledTimes(1)
    releaseSecond()
    expect(socket.emit).toHaveBeenLastCalledWith('frontend:unsubscribe_workspace', { workspace_id: 'ws-1' })
    expect(socket.emit).toHaveBeenCalledTimes(2)
    releaseSecond()
    expect(socket.emit).toHaveBeenCalledTimes(2)
  })

  it('subscribes IDs registered before initial connection and restores them on reconnect', () => {
    service.subscribeToWorkspace('ws-before-connect')
    service.connect()
    expect(socket.emit).not.toHaveBeenCalled()

    socket.connect()
    expect(socket.emit).toHaveBeenCalledWith('frontend:subscribe_workspace', {
      workspace_id: 'ws-before-connect',
    })
    socket.emit.mockClear()
    socket.disconnect()
    socket.connect()
    expect(socket.emit).toHaveBeenCalledTimes(1)
    expect(socket.emit).toHaveBeenCalledWith('frontend:subscribe_workspace', {
      workspace_id: 'ws-before-connect',
    })
  })

  it('attaches events registered before connect and removes only the released callback', () => {
    const first = vi.fn()
    const second = vi.fn()
    const releaseFirst = service.onEvent('workspace:error', first)
    const releaseSecond = service.onEvent('workspace:error', second)

    service.connect()
    socket.trigger('workspace:error', { workspace_id: 'ws-1', task_id: 't-1', error: 'failed' })
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(1)

    releaseFirst()
    socket.trigger('workspace:error', { workspace_id: 'ws-1', task_id: 't-1', error: 'again' })
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(2)
    releaseSecond()
  })

  it('allows new event registrations after disconnect and clears old callback state', () => {
    const oldHandler = vi.fn()
    service.connect()
    service.onEvent('workspace:error', oldHandler)
    service.disconnect()
    expect(oldHandler).not.toHaveBeenCalled()

    const nextSocket = makeSocket()
    io.mockReturnValue(nextSocket)
    const newHandler = vi.fn()
    service.onEvent('workspace:error', newHandler)
    service.connect()
    nextSocket.connect()
    nextSocket.trigger('workspace:error', { workspace_id: 'ws-2', task_id: 't-2', error: 'new' })
    expect(newHandler).toHaveBeenCalledTimes(1)
  })
})
