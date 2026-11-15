import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { VueWrapper } from '@vue/test-utils'

import WorkspaceTerminal from './WorkspaceTerminal.vue'
import * as socket from '@/services/socket'
import * as workspacesApi from '@/services/workspaces.api'

type Handler = (data: Record<string, string>) => void
const handlers = new Map<string, Set<Handler>>()
const reconnects = new Set<() => void>()
const rooms = new Map<string, number>()

function emit(event: string, data: Record<string, string>): void {
  handlers.get(event)?.forEach((handler) => handler(data))
}

vi.mock('@/services/socket', () => ({
  isConnected: { value: true },
  getConnectionId: vi.fn(() => 'connection-1'),
  subscribeToWorkspace: vi.fn((workspaceId: string) => {
    rooms.set(workspaceId, (rooms.get(workspaceId) ?? 0) + 1)
    return () => rooms.set(workspaceId, (rooms.get(workspaceId) ?? 1) - 1)
  }),
  onEvent: vi.fn((event: string, handler: Handler) => {
    if (!handlers.has(event)) handlers.set(event, new Set())
    handlers.get(event)!.add(handler)
    return () => handlers.get(event)?.delete(handler)
  }),
  onReconnect: vi.fn((callback: () => void) => {
    reconnects.add(callback)
    return () => reconnects.delete(callback)
  }),
  sendTerminalInput: vi.fn(),
  sendTerminalResize: vi.fn(),
  sendTerminalClose: vi.fn(),
}))

vi.mock('@/services/workspaces.api', () => ({
  startTerminal: vi.fn(),
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 80
    rows = 24
    loadAddon(): void {}
    open(): void {}
    onData(): void {}
    onResize(): void {}
    write(): void {}
    writeln(): void {}
    focus(): void {}
    dispose(): void {}
  },
}))

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit(): void {}
  },
}))

const startTerminal = vi.mocked(workspacesApi.startTerminal)
const sendTerminalClose = vi.mocked(socket.sendTerminalClose)
const wrappers: VueWrapper[] = []

function mountTerminal(): VueWrapper {
  const wrapper = mount(WorkspaceTerminal, {
    props: { workspaceId: 'ws-1' },
    global: {
      stubs: {
        Button: { template: '<button v-bind="$attrs"><slot /></button>' },
        LoadingSpinner: { template: '<div />' },
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

function setup(): void {
  setActivePinia(createPinia())
  handlers.clear()
  reconnects.clear()
  rooms.clear()
  vi.clearAllMocks()
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe(): void {}
      disconnect(): void {}
    },
  )
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    callback(0)
    return 0
  })
}

function teardown(): void {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.unstubAllGlobals()
}

describe('WorkspaceTerminal start failures', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    handlers.clear()
    vi.clearAllMocks()
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe(): void {}
        disconnect(): void {}
      },
    )
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callback(0)
      return 0
    })
  })

  afterEach(() => {
    wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.unstubAllGlobals()
  })

  it('shows the runner error with a retry instead of spinning forever', async () => {
    startTerminal.mockResolvedValue({ task_id: 'task-1' })
    const wrapper = mountTerminal()
    await flushPromises()
    expect(wrapper.text()).toContain('Starting terminal')

    emit('workspace:error', { workspace_id: 'ws-1', task_id: 'task-1', error: 'SSH down' })
    await flushPromises()

    expect(wrapper.text()).not.toContain('Starting terminal')
    expect(wrapper.text()).toContain('SSH down')
    expect(wrapper.text()).toContain('Retry')
  })

  it('fails the start when the error arrives before the start request resolves', async () => {
    let resolve!: (value: { task_id: string }) => void
    startTerminal.mockReturnValue(new Promise((done) => (resolve = done)))
    const wrapper = mountTerminal()
    await flushPromises()

    emit('workspace:error', { workspace_id: 'ws-1', task_id: 'task-1', error: 'SSH down' })
    resolve({ task_id: 'task-1' })
    await flushPromises()

    expect(wrapper.text()).toContain('SSH down')
    expect(wrapper.text()).not.toContain('Starting terminal')
  })

  it('ignores errors of other workspaces and other tasks', async () => {
    startTerminal.mockResolvedValue({ task_id: 'task-1' })
    const wrapper = mountTerminal()
    await flushPromises()

    emit('workspace:error', { workspace_id: 'ws-2', task_id: 'task-1', error: 'other ws' })
    emit('workspace:error', { workspace_id: 'ws-1', task_id: 'task-0', error: 'old task' })
    await flushPromises()

    expect(wrapper.text()).toContain('Starting terminal')
    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'term-1', task_id: 'task-1' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Starting terminal')
    expect(wrapper.text()).not.toContain('Retry')
  })
})

describe('WorkspaceTerminal shell ownership', () => {
  beforeEach(setup)
  afterEach(teardown)

  it('binds the shell to this connection and adopts only its own start', async () => {
    let resolve!: (value: { task_id: string }) => void
    startTerminal.mockReturnValue(new Promise((done) => (resolve = done)))
    const wrapper = mountTerminal()
    await flushPromises()
    expect(startTerminal).toHaveBeenCalledWith('ws-1', 80, 24, 'connection-1')

    // Another tab's shell of the same workspace, then this start, both early.
    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'theirs', task_id: 'other' })
    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'mine', task_id: 'task-1' })
    resolve({ task_id: 'task-1' })
    await flushPromises()

    wrapper.unmount()
    expect(sendTerminalClose.mock.calls).toEqual([['ws-1', 'mine']])
    expect(rooms.get('ws-1')).toBe(0)
  })

  it('closes a shell that only starts after the panel unmounted', async () => {
    let resolve!: (value: { task_id: string }) => void
    startTerminal.mockReturnValue(new Promise((done) => (resolve = done)))
    const wrapper = mountTerminal()
    await flushPromises()

    wrapper.unmount()
    resolve({ task_id: 'task-1' })
    await flushPromises()
    expect(sendTerminalClose).not.toHaveBeenCalled()

    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'late', task_id: 'task-1' })
    expect(sendTerminalClose.mock.calls).toEqual([['ws-1', 'late']])
  })

  it('replaces the shell after a reconnect instead of leaking it', async () => {
    startTerminal.mockResolvedValueOnce({ task_id: 'task-1' })
    startTerminal.mockResolvedValueOnce({ task_id: 'task-2' })
    const wrapper = mountTerminal()
    await flushPromises()
    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'first', task_id: 'task-1' })
    await flushPromises()

    reconnects.forEach((callback) => callback())
    await flushPromises()
    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'second', task_id: 'task-2' })
    await flushPromises()

    expect(sendTerminalClose.mock.calls).toEqual([['ws-1', 'first']])
    wrapper.unmount()
    expect(sendTerminalClose.mock.calls).toEqual([
      ['ws-1', 'first'],
      ['ws-1', 'second'],
    ])
  })

  it('tells the user when the server closed an idle shell', async () => {
    startTerminal.mockResolvedValue({ task_id: 'task-1' })
    const wrapper = mountTerminal()
    await flushPromises()
    emit('terminal:started', { workspace_id: 'ws-1', terminal_id: 'term-1', task_id: 'task-1' })
    await flushPromises()

    emit('terminal:closed', { workspace_id: 'ws-1', terminal_id: 'term-1', reason: 'idle' })
    await flushPromises()

    expect(wrapper.text()).toContain('Terminal closed after inactivity')
    expect(wrapper.find('[data-testid="terminal-reconnect"]').exists()).toBe(true)
    wrapper.unmount()
    expect(sendTerminalClose).not.toHaveBeenCalled()
  })
})
