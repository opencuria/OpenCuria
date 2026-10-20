import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { VueWrapper } from '@vue/test-utils'

import WorkspaceTerminal from './WorkspaceTerminal.vue'
import * as workspacesApi from '@/services/workspaces.api'

type Handler = (data: Record<string, string>) => void
const handlers = new Map<string, Set<Handler>>()

function emit(event: string, data: Record<string, string>): void {
  handlers.get(event)?.forEach((handler) => handler(data))
}

vi.mock('@/services/socket', () => ({
  onEvent: vi.fn((event: string, handler: Handler) => {
    if (!handlers.has(event)) handlers.set(event, new Set())
    handlers.get(event)!.add(handler)
    return () => handlers.get(event)?.delete(handler)
  }),
  onReconnect: vi.fn(() => () => {}),
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
