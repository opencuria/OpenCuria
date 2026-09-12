import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

import { useDesktopSession } from './useDesktopSession'
import { useDesktopStore } from '@/stores/desktop'
import * as workspacesApi from '@/services/workspaces.api'

vi.mock('@/services/workspaces.api', () => ({
  getDesktopStatus: vi.fn(),
  startDesktop: vi.fn(),
  stopDesktop: vi.fn(),
  takeDesktopControl: vi.fn(),
  writeDesktopClipboard: vi.fn(),
  readDesktopClipboard: vi.fn(),
}))

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ success: vi.fn(), error: vi.fn() }),
}))

type Handler = (data: Record<string, unknown>) => void

const handlers = new Map<string, Handler>()

vi.mock('@/services/socket', () => ({
  onEvent: vi.fn((event: string, cb: Handler) => {
    handlers.set(event, cb)
    return () => {
      handlers.delete(event)
    }
  }),
}))

const getDesktopStatus = vi.mocked(workspacesApi.getDesktopStatus)
const stopDesktopApi = vi.mocked(workspacesApi.stopDesktop)

function emit(event: string, data: Record<string, unknown>): void {
  handlers.get(event)?.(data)
}

describe('useDesktopSession lifecycle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    handlers.clear()
    vi.clearAllMocks()
    stopDesktopApi.mockResolvedValue({ task_id: 'task-stop' })
  })

  it('keeps the connected iframe on viewer_released while computer-use holds', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    const session = useDesktopSession(ref('ws-1'))
    session.setupSocketListeners()

    emit('desktop:viewer_released', {
      workspace_id: 'ws-1',
      task_id: 'task-1',
      computer_use_active: true,
    })

    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
    expect(store.computerUseActive).toBe(true)
    session.cleanupSocketListeners()
  })

  it('disconnects on viewer_released when nothing still holds the process', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    const session = useDesktopSession(ref('ws-1'))
    session.setupSocketListeners()

    emit('desktop:viewer_released', {
      workspace_id: 'ws-1',
      task_id: 'task-1',
      computer_use_active: false,
    })

    expect(store.isConnected).toBe(false)
    expect(store.proxyUrl).toBeNull()
    expect(store.computerUseActive).toBe(false)
    session.cleanupSocketListeners()
  })

  it('syncs computer-use false on duplicate desktop:started for the same proxy URL (no remount)', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)
    const session = useDesktopSession(ref('ws-1'))
    session.setupSocketListeners()

    const urlBefore = store.proxyUrl
    emit('desktop:started', {
      workspace_id: 'ws-1',
      task_id: 'task-1',
      proxy_url: '/ws/desktop/ws-1/',
      computer_use_active: false,
    })

    expect(store.proxyUrl).toBe(urlBefore)
    expect(store.isConnected).toBe(true)
    expect(store.computerUseActive).toBe(false)
    session.cleanupSocketListeners()
  })

  it('syncs computer-use false on desktop:started with a new proxy URL', () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)
    const session = useDesktopSession(ref('ws-1'))
    session.setupSocketListeners()

    emit('desktop:started', {
      workspace_id: 'ws-1',
      task_id: 'task-1',
      proxy_url: '/ws/desktop/ws-1/?v=2',
      computer_use_active: false,
    })

    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/?v=2')
    expect(store.isConnected).toBe(true)
    expect(store.computerUseActive).toBe(false)
    session.cleanupSocketListeners()
  })

  it('stopDesktop falls back to status when the socket event is missed', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    getDesktopStatus.mockResolvedValue({
      active: false,
      proxy_url: null,
      viewer_held: false,
      computer_use_active: false,
    })
    const session = useDesktopSession(ref('ws-1'))

    await expect(session.stopDesktop()).resolves.toBe(true)

    expect(stopDesktopApi).toHaveBeenCalledWith('ws-1')
    expect(store.isConnected).toBe(false)
  })

  it('stopDesktop keeps the session read-only when computer-use still holds', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)
    getDesktopStatus.mockResolvedValue({
      active: true,
      proxy_url: '/ws/desktop/ws-1/',
      viewer_held: false,
      computer_use_active: true,
    })
    const session = useDesktopSession(ref('ws-1'))

    await expect(session.stopDesktop()).resolves.toBe(true)

    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
    expect(store.computerUseActive).toBe(true)
  })

  it('stopDesktop polls through a stale viewer lease until inactive', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    getDesktopStatus
      .mockResolvedValueOnce({
        active: true,
        proxy_url: '/ws/desktop/ws-1/',
        viewer_held: true,
        computer_use_active: false,
      })
      .mockResolvedValueOnce({
        active: false,
        proxy_url: null,
        viewer_held: false,
        computer_use_active: false,
      })
    const sleeps: number[] = []
    const session = useDesktopSession(ref('ws-1'), {
      sleep: (ms: number) => {
        sleeps.push(ms)
        return Promise.resolve()
      },
      stopPollIntervalMs: 250,
      stopPollTimeoutMs: 5000,
    })

    await expect(session.stopDesktop()).resolves.toBe(true)

    expect(getDesktopStatus).toHaveBeenCalledTimes(2)
    expect(sleeps).toEqual([250])
    expect(store.isConnected).toBe(false)
    expect(store.proxyUrl).toBeNull()
  })

  it('stopDesktop keeps viewer released + CU active after a stale first read', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    getDesktopStatus
      .mockResolvedValueOnce({
        active: true,
        proxy_url: '/ws/desktop/ws-1/',
        viewer_held: true,
        computer_use_active: true,
      })
      .mockResolvedValueOnce({
        active: true,
        proxy_url: '/ws/desktop/ws-1/',
        viewer_held: false,
        computer_use_active: true,
      })
    const session = useDesktopSession(ref('ws-1'), {
      sleep: () => Promise.resolve(),
      stopPollIntervalMs: 250,
      stopPollTimeoutMs: 5000,
    })

    await expect(session.stopDesktop()).resolves.toBe(true)

    expect(getDesktopStatus).toHaveBeenCalledTimes(2)
    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
    expect(store.computerUseActive).toBe(true)
  })

  it('stopDesktop disconnects locally after repeated poll errors when CU unknown', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    getDesktopStatus.mockRejectedValue(new Error('net down'))
    const sleeps: number[] = []
    const session = useDesktopSession(ref('ws-1'), {
      sleep: (ms: number) => {
        sleeps.push(ms)
        return Promise.resolve()
      },
      stopPollIntervalMs: 250,
      stopPollTimeoutMs: 5000,
    })

    await expect(session.stopDesktop()).resolves.toBe(true)

    expect(getDesktopStatus).toHaveBeenCalledTimes(3)
    expect(sleeps).toEqual([250, 250])
    expect(store.isConnected).toBe(false)
    expect(store.proxyUrl).toBeNull()
  })

  it('stopDesktop keeps the iframe when polls fail but CU is known active', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)
    getDesktopStatus.mockRejectedValue(new Error('net down'))
    const session = useDesktopSession(ref('ws-1'), {
      sleep: () => Promise.resolve(),
      stopPollIntervalMs: 250,
      stopPollTimeoutMs: 5000,
    })

    await expect(session.stopDesktop()).resolves.toBe(true)

    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
    expect(store.computerUseActive).toBe(true)
  })

  it('stopDesktop never overwrites a workspace switch during the poll', async () => {
    const workspaceId = ref('ws-1')
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    getDesktopStatus.mockImplementation(async () => {
      workspaceId.value = 'ws-2'
      return {
        active: false,
        proxy_url: null,
        viewer_held: false,
        computer_use_active: false,
      }
    })
    const session = useDesktopSession(workspaceId, {
      sleep: () => Promise.resolve(),
    })

    await expect(session.stopDesktop()).resolves.toBe(true)

    // Stale ws-1 status must not touch the store of the new workspace.
    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
  })

  it('stopDesktop after cleanupSocketListeners does not mutate the store', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    type DesktopStatus = Awaited<ReturnType<typeof getDesktopStatus>>
    let resolveStatus!: (status: DesktopStatus) => void
    getDesktopStatus.mockImplementation(
      () =>
        new Promise<DesktopStatus>((resolve) => {
          resolveStatus = resolve
        }),
    )
    const session = useDesktopSession(ref('ws-1'), {
      sleep: () => Promise.resolve(),
    })

    const pending = session.stopDesktop()
    // Let the POST resolve and the first status poll start so the mock
    // implementation (which captures resolveStatus) is installed.
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    // Simulate unmount/parallel stop invalidating the in-flight poll.
    session.cleanupSocketListeners()
    resolveStatus({
      active: false,
      proxy_url: null,
      viewer_held: false,
      computer_use_active: false,
    })

    await expect(pending).resolves.toBe(true)

    // The invalidated poll must leave the store untouched.
    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
  })

  it('stopDesktop invalidated during the stop POST never polls or mutates', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    let resolveStop!: (value: { task_id: string }) => void
    stopDesktopApi.mockImplementationOnce(
      () =>
        new Promise<{ task_id: string }>((resolve) => {
          resolveStop = resolve
        }),
    )
    const session = useDesktopSession(ref('ws-1'), {
      sleep: () => Promise.resolve(),
    })

    const pending = session.stopDesktop()
    expect(stopDesktopApi).toHaveBeenCalledWith('ws-1')
    // Simulate unmount/parallel stop invalidating the in-flight POST.
    session.cleanupSocketListeners()
    resolveStop({ task_id: 'task-stop' })

    await expect(pending).resolves.toBe(true)

    // The invalidated POST must not trigger any status poll or mutation.
    expect(getDesktopStatus).not.toHaveBeenCalled()
    expect(store.isConnected).toBe(true)
    expect(store.proxyUrl).toBe('/ws/desktop/ws-1/')
  })

  it('stopDesktopIfActive disconnects locally even when the stop POST fails', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    stopDesktopApi.mockRejectedValueOnce(new Error('boom'))
    const session = useDesktopSession(ref('ws-1'))

    await session.stopDesktopIfActive('ws-1')

    expect(store.isConnected).toBe(false)
    expect(store.proxyUrl).toBeNull()
  })
})
