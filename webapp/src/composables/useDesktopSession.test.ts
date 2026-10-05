import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { useDesktopSession } from './useDesktopSession'
import {
  createViewerClientId,
  desktopViewerClientId,
  desktopViewerSession,
  refreshDesktopViewer,
  RELEASE_GRACE_MS,
} from './useDesktopSessionCoordinator'
import { useDesktopStore } from '@/stores/desktop'
import * as api from '@/services/workspaces.api'

vi.mock('@/services/workspaces.api', () => ({
  getDesktopStatus: vi.fn(),
  startDesktop: vi.fn(),
  stopDesktop: vi.fn(),
  renewDesktop: vi.fn(),
  takeDesktopControl: vi.fn(),
  writeDesktopClipboard: vi.fn(),
  readDesktopClipboard: vi.fn(),
}))
vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ success: vi.fn(), error: vi.fn() }),
}))
const handlers = new Map<string, (data: { workspace_id: string }) => void>()
vi.mock('@/services/socket', () => ({
  onEvent: vi.fn((event: string, cb: (data: { workspace_id: string }) => void) => {
    handlers.set(event, cb)
    return () => handlers.delete(event)
  }),
}))
let count = 0
const scopes: ReturnType<typeof effectScope>[] = []
function surface(id: string, observer = false) {
  const scope = effectScope()
  scopes.push(scope)
  const session = scope.run(() => useDesktopSession(ref(id), { observer }))!
  return { scope, session }
}
function status(id: string, state = 'held', epoch = 'epoch-1') {
  return {
    active: true,
    proxy_url: `/ws/desktop/${id}/`,
    viewer_held: true,
    computer_use_active: false,
    mcp_active: true,
    holder_count: 2,
    viewer_lease_state: state,
    revision: desktopViewerSession(id).revision,
    epoch,
  }
}
beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  handlers.clear()
  vi.useFakeTimers()
  vi.mocked(api.startDesktop).mockResolvedValue({ task_id: 'start' })
  vi.mocked(api.stopDesktop).mockResolvedValue({ task_id: 'stop' })
  vi.mocked(api.getDesktopStatus).mockImplementation(async (id) => status(id))
  vi.mocked(api.renewDesktop).mockImplementation(async (id) => status(id))
})
afterEach(async () => {
  scopes.splice(0).forEach((scope) => scope.stop())
  await flushPromises()
  vi.useRealTimers()
})

describe('per-tab desktop viewer intents', () => {
  it('uses one in-memory UUID and own status query, renews held intent', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    await session.startDesktop()
    expect(desktopViewerClientId).toMatch(/^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/)
    expect(api.startDesktop).toHaveBeenCalledWith(id, {
      viewer_client_id: desktopViewerClientId,
      intent_revision: 1,
    })
    expect(api.getDesktopStatus).toHaveBeenCalledWith(id, desktopViewerClientId)
    expect(useDesktopStore().viewerOwned).toBe(true)
    await vi.advanceTimersByTimeAsync(45_000)
    expect(api.renewDesktop).toHaveBeenCalledWith(id, {
      viewer_client_id: desktopViewerClientId,
      intent_revision: 1,
    })
  })

  it('builds a UUID when crypto.randomUUID is missing', () => {
    const cryptoObj = globalThis.crypto as unknown as {
      randomUUID?: () => string
      getRandomValues?: (bytes: Uint8Array) => Uint8Array
    }
    const randomUUID = cryptoObj.randomUUID
    const getRandomValues = cryptoObj.getRandomValues
    const uuid = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/
    cryptoObj.randomUUID = undefined
    try {
      expect(createViewerClientId()).toMatch(uuid)
      cryptoObj.getRandomValues = undefined
      expect(createViewerClientId()).toMatch(uuid)
    } finally {
      cryptoObj.randomUUID = randomUUID
      cryptoObj.getRandomValues = getRandomValues
    }
  })

  it('refcounts later surfaces and sends only final release', async () => {
    const id = `viewer-${++count}`
    const first = surface(id)
    await first.session.startDesktop()
    const second = surface(id)
    await second.session.startDesktop()
    expect(api.startDesktop).toHaveBeenCalledTimes(1)
    first.scope.stop()
    await flushPromises()
    expect(api.stopDesktop).not.toHaveBeenCalled()
    second.scope.stop()
    await flushPromises()
    expect(api.stopDesktop).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS)
    expect(api.stopDesktop).toHaveBeenCalledExactlyOnceWith(id, {
      viewer_client_id: desktopViewerClientId,
      intent_revision: 1,
    })
  })

  it('hands the intent to a surface remounted within the grace period', async () => {
    const id = `viewer-${++count}`
    const first = surface(id)
    await first.session.startDesktop()
    first.scope.stop()
    await flushPromises()
    const second = surface(id)
    await second.session.startDesktop()
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS * 2)
    expect(api.stopDesktop).not.toHaveBeenCalled()
    expect(api.startDesktop).toHaveBeenCalledTimes(1)
    expect(desktopViewerSession(id).revision).toBe(1)
    expect(desktopViewerSession(id).wanted).toBe(true)
  })

  it('stops immediately on explicit stop even while a release is deferred', async () => {
    const id = `viewer-${++count}`
    const first = surface(id)
    await first.session.startDesktop()
    first.scope.stop()
    await flushPromises()
    expect(api.stopDesktop).not.toHaveBeenCalled()
    await first.session.stopDesktop()
    expect(api.stopDesktop).toHaveBeenCalledExactlyOnceWith(id, {
      viewer_client_id: desktopViewerClientId,
      intent_revision: 1,
    })
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS * 2)
    expect(api.stopDesktop).toHaveBeenCalledTimes(1)
  })

  it('tombstones after the grace period before a delayed start finishes and ignores late completion', async () => {
    const id = `viewer-${++count}`
    let finish!: (value: { task_id: string }) => void
    vi.mocked(api.startDesktop).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve
      }),
    )
    const first = surface(id)
    const pending = first.session.startDesktop()
    first.scope.stop()
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS)
    expect(api.stopDesktop).toHaveBeenCalledTimes(1)
    const second = surface(id)
    await second.session.startDesktop()
    expect(api.startDesktop).toHaveBeenLastCalledWith(id, {
      viewer_client_id: desktopViewerClientId,
      intent_revision: 2,
    })
    finish({ task_id: 'old' })
    await pending
    expect(desktopViewerSession(id).revision).toBe(2)
    expect(desktopViewerSession(id).wanted).toBe(true)
  })

  it('never inherits membership from global active broadcasts or observer state', async () => {
    const id = `viewer-${++count}`
    vi.mocked(api.getDesktopStatus).mockResolvedValue({
      ...status(id),
      viewer_lease_state: 'unknown',
      revision: 0,
    })
    const { session } = surface(id, true)
    session.setupSocketListeners()
    await session.startDesktop()
    handlers.get('desktop:started')?.({ workspace_id: id })
    await flushPromises()
    const store = useDesktopStore()
    expect(store.isConnected).toBe(true)
    expect(store.mcpActive).toBe(true)
    expect(store.holderCount).toBe(2)
    expect(store.viewerOwned).toBe(false)
    expect(store.intentRevision).toBeNull()
    await vi.advanceTimersByTimeAsync(90_000)
    await session.stopDesktop()
    expect(api.startDesktop).not.toHaveBeenCalled()
    expect(api.stopDesktop).not.toHaveBeenCalled()
    expect(api.renewDesktop).not.toHaveBeenCalled()
  })

  it('reacquires higher revision after expiry and epoch change rather than renewing ended lease', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    await session.startDesktop()
    vi.mocked(api.getDesktopStatus).mockImplementationOnce(async () => status(id, 'expired'))
    await refreshDesktopViewer(id)
    expect(desktopViewerSession(id).revision).toBe(2)
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id, 'held', 'epoch-2'))
    await refreshDesktopViewer(id)
    expect(desktopViewerSession(id).revision).toBe(3)
    expect(api.renewDesktop).not.toHaveBeenCalled()
  })

  it('does not renew reserved or globally held leases, retries observation on outage', async () => {
    const id = `viewer-${++count}`
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id, 'reserved'))
    const { session } = surface(id)
    await session.startDesktop()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(api.renewDesktop).not.toHaveBeenCalled()
    vi.mocked(api.getDesktopStatus).mockRejectedValueOnce(new Error('offline'))
    await refreshDesktopViewer(id)
    expect(api.startDesktop).toHaveBeenCalledTimes(1)
    expect(desktopViewerSession(id).error).toBe('offline')
  })

  it('does not release an active desktop merely inherited from Pinia', async () => {
    const id = `viewer-${++count}`
    useDesktopStore().setConnected(id, `/ws/desktop/${id}/`)
    const { scope } = surface(id)
    scope.stop()
    await flushPromises()
    expect(api.stopDesktop).not.toHaveBeenCalled()
  })

  it('ignores an old own-status response after a newer acquire', async () => {
    const id = `viewer-${++count}`
    let resolveStatus!: (value: ReturnType<typeof status>) => void
    vi.mocked(api.getDesktopStatus).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStatus = resolve
      }),
    )
    const { session } = surface(id)
    const first = session.startDesktop()
    await flushPromises()
    await session.stopDesktop()
    await session.startDesktop()
    resolveStatus({ ...status(id), revision: 1, viewer_lease_state: 'expired' })
    await first
    expect(desktopViewerSession(id).revision).toBe(2)
    expect(useDesktopStore().viewerOwned).toBe(true)
    expect(api.startDesktop).toHaveBeenCalledTimes(2)
  })

  it('retries a failed final tombstone with the same revision', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    await session.startDesktop()
    vi.mocked(api.stopDesktop).mockRejectedValueOnce(new Error('offline'))
    expect(await session.stopDesktop()).toBe(false)
    await vi.advanceTimersByTimeAsync(45_000)
    expect(api.stopDesktop).toHaveBeenCalledTimes(2)
    expect(api.stopDesktop).toHaveBeenLastCalledWith(id, {
      viewer_client_id: desktopViewerClientId,
      intent_revision: 1,
    })
    expect(api.startDesktop).toHaveBeenCalledTimes(1)
  })

  it('sends release after failed start and reports stop failure without claiming ownership', async () => {
    const id = `viewer-${++count}`
    vi.mocked(api.startDesktop).mockRejectedValueOnce(new Error('permission denied'))
    const { session } = surface(id)
    await session.startDesktop()
    expect(session.error.value).toBe('permission denied')
    vi.mocked(api.stopDesktop).mockRejectedValueOnce(new Error('offline'))
    expect(await session.stopDesktop()).toBe(false)
    expect(useDesktopStore().viewerOwned).toBe(false)
    expect(api.stopDesktop).toHaveBeenCalledTimes(1)
  })
})

describe('viewer observation fencing and bounded startup', () => {
  it('retries reopen only after older closing intent is conclusively ended', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    await session.startDesktop()
    await session.stopDesktop()
    vi.mocked(api.getDesktopStatus).mockResolvedValueOnce({
      ...status(id, 'closing'),
      revision: 1,
    })
    await session.startDesktop()
    expect(api.startDesktop).toHaveBeenCalledTimes(2)
    expect(desktopViewerSession(id).revision).toBe(2)
    vi.mocked(api.getDesktopStatus).mockResolvedValueOnce({
      ...status(id, 'released'),
      revision: 1,
    })
    await refreshDesktopViewer(id)
    expect(api.startDesktop).toHaveBeenCalledTimes(3)
    expect(desktopViewerSession(id).revision).toBe(3)
    expect(desktopViewerSession(id).leaseState).toBe('held')
    expect(desktopViewerSession(id).connecting).toBe(false)
  })

  it('bounds asynchronous rejected start retries and unknown startup polling', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    vi.mocked(api.getDesktopStatus).mockResolvedValue({
      ...status(id, 'released'),
      revision: 0,
    })
    await session.startDesktop()
    expect(api.startDesktop).toHaveBeenCalledTimes(3)
    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(desktopViewerSession(id).error).toContain('could not be confirmed')
    const other = `viewer-${++count}`
    vi.mocked(api.getDesktopStatus).mockResolvedValue({
      ...status(other, 'unknown'),
      revision: 0,
    })
    await surface(other).session.startDesktop()
    await vi.advanceTimersByTimeAsync(31_000)
    expect(desktopViewerSession(other).connecting).toBe(false)
    expect(desktopViewerSession(other).timer).toBeNull()
    expect(desktopViewerSession(other).error).toContain('could not be confirmed')
  })

  it('ignores earlier same-revision state and epoch observations', async () => {
    const id = `viewer-${++count}`
    await surface(id).session.startDesktop()
    let resolveOld!: (value: ReturnType<typeof status>) => void
    vi.mocked(api.getDesktopStatus).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve
      }),
    )
    const old = refreshDesktopViewer(id)
    await refreshDesktopViewer(id)
    resolveOld(status(id, 'expired', 'stale-epoch'))
    await old
    expect(desktopViewerSession(id).leaseState).toBe('held')
    expect(desktopViewerSession(id).epoch).toBe('epoch-1')
    expect(desktopViewerSession(id).revision).toBe(1)
    expect(api.startDesktop).toHaveBeenCalledTimes(1)
  })
})
