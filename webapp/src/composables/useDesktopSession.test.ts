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
import { copyToClipboard } from '@/lib/clipboard'

const notificationMocks = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))

type ClipboardCopyContent = string | (() => Promise<string>)
type ClipboardCopyOptions = { success?: 'toast' | 'silent'; signal?: AbortSignal }
function clipboardCopyCalls(): [ClipboardCopyContent, string, ClipboardCopyOptions?][] {
  return (
    copyToClipboard as unknown as {
      mock: { calls: [ClipboardCopyContent, string, ClipboardCopyOptions?][] }
    }
  ).mock.calls
}

vi.mock('@/lib/clipboard', () => ({
  copyToClipboard: vi.fn(),
}))
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
  useNotificationStore: () => notificationMocks,
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
  const workspaceId = ref(id)
  const session = scope.run(() => useDesktopSession(workspaceId, { observer }))!
  return { scope, session, workspaceId }
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
  notificationMocks.success.mockReset()
  notificationMocks.error.mockReset()
  handlers.clear()
  vi.useFakeTimers()
  vi.mocked(api.startDesktop).mockResolvedValue({ task_id: 'start' })
  vi.mocked(api.stopDesktop).mockResolvedValue({ task_id: 'stop' })
  vi.mocked(api.getDesktopStatus).mockImplementation(async (id) => status(id))
  vi.mocked(api.renewDesktop).mockImplementation(async (id) => status(id))
  vi.mocked(copyToClipboard).mockImplementation(async () => true)
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
    expect(api.startDesktop).toHaveBeenCalledWith(
      id,
      {
        viewer_client_id: desktopViewerClientId,
        intent_revision: 1,
      },
      expect.any(AbortSignal),
    )
    expect(api.getDesktopStatus).toHaveBeenCalledWith(
      id,
      desktopViewerClientId,
      expect.any(AbortSignal),
    )
    expect(useDesktopStore().viewerOwned).toBe(true)
    await vi.advanceTimersByTimeAsync(45_000)
    expect(api.renewDesktop).toHaveBeenCalledWith(
      id,
      {
        viewer_client_id: desktopViewerClientId,
        intent_revision: 1,
      },
      expect.any(AbortSignal),
    )
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
    expect(api.startDesktop).toHaveBeenLastCalledWith(
      id,
      {
        viewer_client_id: desktopViewerClientId,
        intent_revision: 2,
      },
      expect.any(AbortSignal),
    )
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

describe('VM clipboard copying', () => {
  it('starts the clipboard API with a lazy loader and fetches only when requested', async () => {
    const id = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(id, `/ws/desktop/${id}/`)
    const { session } = surface(id)
    vi.mocked(api.readDesktopClipboard).mockResolvedValue({ text: 'from VM' })

    const copy = session.copyFromVmClipboard()

    expect(copyToClipboard).toHaveBeenCalledOnce()
    expect(copyToClipboard).toHaveBeenCalledWith(
      expect.any(Function),
      'VM clipboard',
      expect.objectContaining({ success: 'silent', signal: expect.any(AbortSignal) }),
    )
    expect(notificationMocks.success).not.toHaveBeenCalled()
    expect(api.readDesktopClipboard).not.toHaveBeenCalled()
    const content = clipboardCopyCalls()[0]?.[0]
    expect(typeof content).toBe('function')
    if (typeof content !== 'function') throw new Error('Expected lazy clipboard loader')
    expect(await content()).toBe('from VM')
    expect(api.readDesktopClipboard).toHaveBeenCalledWith(id)
    expect(await copy).toBe(true)
    expect(notificationMocks.success).toHaveBeenCalledWith('Copied from VM')
  })

  it('keeps iframe shortcut delay inside the lazy VM loader', async () => {
    const id = `viewer-${++count}`
    useDesktopStore().setConnected(id, `/ws/desktop/${id}/`)
    const { session } = surface(id)
    vi.mocked(api.readDesktopClipboard).mockResolvedValue({ text: 'after shortcut' })
    const pending = session.copyFromVmClipboard({ delayMs: 120 })
    const content = clipboardCopyCalls()[0]?.[0]
    if (typeof content !== 'function') throw new Error('Expected lazy clipboard loader')

    expect(api.readDesktopClipboard).not.toHaveBeenCalled()
    const loading = content()
    await vi.advanceTimersByTimeAsync(119)
    expect(api.readDesktopClipboard).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(await loading).toBe('after shortcut')
    expect(await pending).toBe(true)
  })

  it('does not load or copy while busy or computer-use controls the desktop', async () => {
    const id = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(id, `/ws/desktop/${id}/`)
    store.setComputerUseActive(true)
    const { session } = surface(id)

    expect(await session.copyFromVmClipboard()).toBe(false)
    expect(copyToClipboard).not.toHaveBeenCalled()
    expect(api.readDesktopClipboard).not.toHaveBeenCalled()

    store.setComputerUseActive(false)
    let resolveCopy!: (value: boolean) => void
    vi.mocked(copyToClipboard).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveCopy = resolve
      }),
    )
    const pending = session.copyFromVmClipboard()
    expect(session.clipboardBusy.value).toBe(true)
    expect(await session.copyFromVmClipboard()).toBe(false)
    resolveCopy(true)
    expect(await pending).toBe(true)
    expect(session.clipboardBusy.value).toBe(false)
  })

  it('aborts the pending manual copy request on workspace switch and releases busy', async () => {
    const id = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(id, `/ws/desktop/${id}/`)
    const { session, workspaceId } = surface(id)
    const requestCopy = vi.fn(
      (_text: string | Promise<string>, _label: string, signal?: AbortSignal) =>
        new Promise<boolean>((resolve) => {
          signal?.addEventListener('abort', () => resolve(false), { once: true })
        }),
    )
    vi.mocked(copyToClipboard).mockImplementation(async (source, _label, copyOptions) => {
      if (typeof source === 'function') await source()
      return requestCopy('VM text', 'VM clipboard', copyOptions?.signal)
    })
    vi.mocked(api.readDesktopClipboard).mockResolvedValue({ text: 'VM text' })

    const pending = session.copyFromVmClipboard()
    await flushPromises()
    const signal = clipboardCopyCalls()[0]?.[2]?.signal
    expect(signal).toBeDefined()
    expect(signal?.aborted).toBe(false)
    expect(session.clipboardBusy.value).toBe(true)

    workspaceId.value = `${id}-next`
    expect(signal?.aborted).toBe(true)
    expect(await pending).toBe(false)
    expect(session.clipboardBusy.value).toBe(false)
    expect(requestCopy).toHaveBeenCalledOnce()
    expect(notificationMocks.success).not.toHaveBeenCalled()
  })

  it('aborts only this surface operation on disposal without aborting another surface', async () => {
    const firstId = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(firstId, `/ws/desktop/${firstId}/`)
    const first = surface(firstId)
    const second = surface(firstId)
    const resolveRequests: Array<(value: boolean) => void> = []
    vi.mocked(copyToClipboard).mockImplementation(async (source, _label, copyOptions) => {
      if (typeof source === 'function') await source()
      return new Promise<boolean>((resolve) => {
        resolveRequests.push(resolve)
        copyOptions?.signal?.addEventListener('abort', () => resolve(false), { once: true })
      })
    })

    vi.mocked(api.readDesktopClipboard).mockResolvedValue({ text: 'manual fallback text' })
    const firstPending = first.session.copyFromVmClipboard()
    const firstSignal = clipboardCopyCalls()[0]?.[2]?.signal
    const secondPending = second.session.copyFromVmClipboard()
    const secondSignal = clipboardCopyCalls()[1]?.[2]?.signal
    await flushPromises()
    expect(firstSignal).toBeDefined()
    expect(secondSignal).toBeDefined()
    expect(firstSignal).not.toBe(secondSignal)

    first.scope.stop()
    expect(firstSignal?.aborted).toBe(true)
    expect(secondSignal?.aborted).toBe(false)
    expect(await firstPending).toBe(false)
    expect(second.session.clipboardBusy.value).toBe(true)

    resolveRequests[1]?.(true)
    expect(await secondPending).toBe(true)
    expect(notificationMocks.success).toHaveBeenCalledOnce()
    expect(second.session.clipboardBusy.value).toBe(false)
  })

  it('rejects stale loads after workspace change or disposal', async () => {
    const id = `viewer-${++count}`
    useDesktopStore().setConnected(id, `/ws/desktop/${id}/`)
    const { scope, session, workspaceId } = surface(id)
    const pending = session.copyFromVmClipboard()
    const content = clipboardCopyCalls()[0]?.[0]
    if (typeof content !== 'function') throw new Error('Expected lazy clipboard loader')
    workspaceId.value = `${id}-other`

    await expect(content()).rejects.toThrow('no longer current')
    scope.stop()
    expect(api.readDesktopClipboard).not.toHaveBeenCalled()
    expect(await pending).toBe(false)
    expect(session.clipboardBusy.value).toBe(false)
  })

  it('rejects in-flight VM data if the workspace changes or the session is disposed', async () => {
    const id = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(id, `/ws/desktop/${id}/`)
    const first = surface(id)
    let resolveFirst!: (value: { text: string }) => void
    vi.mocked(api.readDesktopClipboard).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFirst = resolve
      }),
    )
    const firstPending = first.session.copyFromVmClipboard()
    const firstContent = clipboardCopyCalls()[0]?.[0]
    if (typeof firstContent !== 'function') throw new Error('Expected lazy clipboard loader')
    const staleWorkspaceLoad = firstContent()
    await flushPromises()
    first.workspaceId.value = `${id}-next`
    resolveFirst({ text: 'stale workspace data' })
    await expect(staleWorkspaceLoad).rejects.toThrow('no longer current')
    await firstPending

    const otherId = `${id}-disposed`
    store.setConnected(otherId, `/ws/desktop/${otherId}/`)
    const second = surface(otherId)
    let resolveSecond!: (value: { text: string }) => void
    vi.mocked(api.readDesktopClipboard).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSecond = resolve
      }),
    )
    const secondPending = second.session.copyFromVmClipboard()
    const secondContent = clipboardCopyCalls()[1]?.[0]
    if (typeof secondContent !== 'function') throw new Error('Expected lazy clipboard loader')
    const disposedLoad = secondContent()
    await flushPromises()
    second.scope.stop()
    resolveSecond({ text: 'disposed session data' })
    await expect(disposedLoad).rejects.toThrow('no longer current')
    await secondPending
  })

  it('keeps fetch failures generic and lets the shared clipboard helper handle them', async () => {
    const id = `viewer-${++count}`
    useDesktopStore().setConnected(id, `/ws/desktop/${id}/`)
    const { session } = surface(id)
    vi.mocked(api.readDesktopClipboard).mockRejectedValue(new Error('secret network detail'))
    const pending = session.copyFromVmClipboard()
    const content = clipboardCopyCalls()[0]?.[0]
    if (typeof content !== 'function') throw new Error('Expected lazy clipboard loader')

    await expect(content()).rejects.toThrow('Unable to load VM clipboard')
    expect(await pending).toBe(true)
  })
})

describe('viewer observation fencing and bounded startup', () => {
  it('waits for route readiness after the own lease is held without renewing every poll', async () => {
    const id = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(id, `/ws/desktop/${id}/`)
    vi.mocked(api.getDesktopStatus)
      .mockImplementationOnce(async () => ({
        ...status(id, 'held'),
        active: false,
        proxy_url: null,
        revision: desktopViewerSession(id).revision,
      }))
      .mockImplementationOnce(async () => ({
        ...status(id, 'held'),
        revision: desktopViewerSession(id).revision,
      }))
    const { session } = surface(id)

    await session.startDesktop()
    expect(desktopViewerSession(id).leaseState).toBe('held')
    expect(desktopViewerSession(id).connecting).toBe(true)
    expect(store.isConnected).toBe(false)
    expect(store.isConnecting).toBe(true)
    expect(api.renewDesktop).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1_000)

    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(desktopViewerSession(id).startupDeadline).toBe(0)
    expect(store.isConnected).toBe(true)
    expect(store.isConnecting).toBe(false)
    expect(api.getDesktopStatus).toHaveBeenCalledTimes(2)
    expect(api.renewDesktop).not.toHaveBeenCalled()
  })

  it('fails permanent missing route at deadline and permits an explicit retry', async () => {
    const id = `viewer-${++count}`
    const store = useDesktopStore()
    store.setConnected(id, `/ws/desktop/${id}/`)
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => ({
      ...status(id, 'held'),
      active: false,
      proxy_url: null,
    }))
    const { session } = surface(id)

    await session.startDesktop()
    expect(desktopViewerSession(id).connecting).toBe(true)
    expect(store.isConnected).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)

    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(session.error.value).toContain('could not be confirmed')
    expect(store.isConnecting).toBe(false)
    expect(store.isConnected).toBe(false)
    expect(desktopViewerSession(id).status?.active).toBe(false)

    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id, 'held'))
    await session.startDesktop()
    expect(api.startDesktop).toHaveBeenCalledTimes(2)
    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(store.isConnected).toBe(true)
    expect(session.error.value).toBeNull()
  })

  it('aborts a start POST that never settles and permits retry after request timeout', async () => {
    const id = `viewer-${++count}`
    let firstStartSignal: AbortSignal | undefined
    vi.mocked(api.startDesktop)
      .mockImplementationOnce((_workspaceId, _intent, signal) => {
        firstStartSignal = signal
        return new Promise(() => undefined)
      })
      .mockResolvedValueOnce({ task_id: 'retry' })
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => ({
      ...status(id, 'unknown'),
      active: false,
      proxy_url: null,
      revision: desktopViewerSession(id).revision,
    }))
    const { session } = surface(id)

    const firstStart = session.startDesktop()
    await flushPromises()
    expect(api.startDesktop).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(10_000)
    await firstStart
    expect(firstStartSignal?.aborted).toBe(true)
    expect(desktopViewerSession(id).connecting).toBe(true)
    expect(desktopViewerSession(id).pending).toBeNull()
    expect(session.error.value).toBe('Desktop request timed out')

    // Retry before the outer startup deadline: the timed-out POST no longer
    // holds the session's single-flight promise.
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id))
    await session.startDesktop()
    expect(api.startDesktop).toHaveBeenCalledTimes(2)
    expect(api.startDesktop).toHaveBeenLastCalledWith(
      id,
      {
        viewer_client_id: desktopViewerClientId,
        intent_revision: 2,
      },
      expect.any(AbortSignal),
    )
    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(useDesktopStore().viewerOwned).toBe(true)
    void firstStart
  })

  it('ignores a status that succeeds after the startup deadline until explicit retry', async () => {
    const id = `viewer-${++count}`
    let resolveStatus!: (value: ReturnType<typeof status>) => void
    let firstStatusSignal: AbortSignal | undefined
    vi.mocked(api.getDesktopStatus)
      .mockImplementationOnce((_id, _clientId, signal) => {
        firstStatusSignal = signal
        return new Promise((resolve) => {
          resolveStatus = resolve
        })
      })
      .mockImplementation(async (workspaceId) => ({
        ...status(workspaceId, 'unknown'),
        active: false,
        proxy_url: null,
        revision: desktopViewerSession(workspaceId).revision,
      }))
    const { session } = surface(id)
    const store = useDesktopStore()

    const firstStart = session.startDesktop()
    await flushPromises()
    expect(api.getDesktopStatus).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(firstStatusSignal?.aborted).toBe(true)
    expect(session.error.value).toBe('Desktop request timed out')
    expect(store.isConnecting).toBe(true)
    // The response is still pending in the mock after its fetch signal was
    // aborted; deadline reconciliation, not that late response, must win.
    await vi.advanceTimersByTimeAsync(20_000)
    const deadlineStatus = desktopViewerSession(id).status
    expect(deadlineStatus?.active).toBe(false)
    expect(deadlineStatus?.viewer_lease_state).toBe('unknown')
    expect(session.error.value).toContain('could not be confirmed')
    resolveStatus(status(id))
    // Promise.race has already rejected at the request deadline. The abandoned
    // underlying response must not be allowed to mutate the session.
    await flushPromises()
    expect(session.error.value).toContain('could not be confirmed')
    expect(store.isConnecting).toBe(false)
    expect(desktopViewerSession(id).status).toBe(deadlineStatus)

    await firstStart
    await flushPromises()
    expect(desktopViewerSession(id).status).toBe(deadlineStatus)
    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(session.error.value).toContain('could not be confirmed')
    expect(store.viewerOwned).toBe(false)

    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id))
    await session.startDesktop()
    expect(api.startDesktop).toHaveBeenCalledTimes(2)
    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(store.viewerOwned).toBe(true)
  })

  it('re-observes a route that disappears after a lease became ready', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    const store = useDesktopStore()
    await session.startDesktop()
    vi.mocked(api.getDesktopStatus).mockResolvedValueOnce({
      ...status(id, 'held'),
      active: false,
      proxy_url: null,
    })

    await refreshDesktopViewer(id)
    expect(desktopViewerSession(id).connecting).toBe(true)
    expect(store.isConnected).toBe(false)
    expect(store.isConnecting).toBe(true)
    expect(desktopViewerSession(id).startupDeadline).toBeGreaterThan(Date.now())

    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id))
    await vi.advanceTimersByTimeAsync(1_000)
    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(store.isConnected).toBe(true)
    expect(api.startDesktop).toHaveBeenCalledOnce()
  })

  it('observes a transient status outage promptly and clears store connecting at the startup deadline', async () => {
    const id = `viewer-${++count}`
    vi.mocked(api.getDesktopStatus)
      .mockRejectedValueOnce(new Error('temporarily offline'))
      .mockImplementation(async () => ({
        ...status(id, 'unknown'),
        revision: desktopViewerSession(id).revision,
      }))
    const { session } = surface(id)
    const store = useDesktopStore()

    await session.startDesktop()
    expect(api.getDesktopStatus).toHaveBeenCalledTimes(1)
    expect(session.error.value).toBe('temporarily offline')
    expect(store.isConnecting).toBe(true)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(api.getDesktopStatus).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(29_000)

    expect(desktopViewerSession(id).connecting).toBe(false)
    expect(session.error.value).toContain('could not be confirmed')
    expect(store.isConnecting).toBe(false)
    expect(store.viewerOwned).toBe(false)
    expect(api.startDesktop).toHaveBeenCalledTimes(1)
  })

  it('observes after a start transport failure without duplicating the start', async () => {
    const id = `viewer-${++count}`
    vi.mocked(api.startDesktop).mockRejectedValueOnce(new Error('connection reset'))
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id))
    const { session } = surface(id)

    await session.startDesktop()
    expect(session.error.value).toBe('connection reset')
    expect(api.getDesktopStatus).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1_000)
    expect(api.getDesktopStatus).toHaveBeenCalledOnce()
    expect(api.startDesktop).toHaveBeenCalledOnce()
    expect(useDesktopStore().viewerOwned).toBe(true)
  })

  it('times out a hanging renew so manual reconnect settles and can retry', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    const store = useDesktopStore()
    await session.startDesktop()
    let renewSignal: AbortSignal | undefined
    vi.mocked(api.renewDesktop).mockImplementationOnce((_id, _intent, signal) => {
      renewSignal = signal
      return new Promise(() => undefined)
    })
    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id))

    const reconnect = session.handleReconnect()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    await reconnect

    expect(renewSignal?.aborted).toBe(true)
    expect(api.getDesktopStatus).toHaveBeenCalledTimes(2)
    expect(store.viewerGeneration).toBe(1)

    await session.handleReconnect()
    expect(api.renewDesktop).toHaveBeenCalledTimes(2)
    expect(store.viewerGeneration).toBe(2)
  })

  it('times out a hanging reconnect status and allows the next manual retry', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    const store = useDesktopStore()
    await session.startDesktop()
    let statusSignal: AbortSignal | undefined
    vi.mocked(api.getDesktopStatus).mockImplementationOnce((_id, _clientId, signal) => {
      statusSignal = signal
      return new Promise(() => undefined)
    })

    const firstReconnect = session.handleReconnect()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    await firstReconnect

    expect(statusSignal?.aborted).toBe(true)
    expect(store.viewerGeneration).toBe(0)

    vi.mocked(api.getDesktopStatus).mockImplementation(async () => status(id))
    await session.handleReconnect()
    expect(store.viewerGeneration).toBe(1)
    expect(api.startDesktop).toHaveBeenCalledOnce()
  })

  it('keeps a manual reconnect single-flight and ignores status after release', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    const store = useDesktopStore()
    await session.startDesktop()
    let resolveStatus!: (value: ReturnType<typeof status>) => void
    vi.mocked(api.getDesktopStatus).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStatus = resolve
      }),
    )

    const firstReconnect = session.handleReconnect()
    const secondReconnect = session.handleReconnect()
    expect(secondReconnect).toBe(firstReconnect)
    await flushPromises()
    expect(api.getDesktopStatus).toHaveBeenCalledTimes(2)
    expect(store.viewerGeneration).toBe(0)

    await session.stopDesktop()
    resolveStatus(status(id, 'held', 'stale-epoch'))
    await firstReconnect
    expect(store.viewerGeneration).toBe(0)
    expect(store.viewerOwned).toBe(false)
    expect(desktopViewerSession(id).wanted).toBe(false)
  })

  it('does not restart after unmount while startup observation is pending', async () => {
    const id = `viewer-${++count}`
    let resolveStatus!: (value: ReturnType<typeof status>) => void
    vi.mocked(api.getDesktopStatus).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStatus = resolve
      }),
    )
    const { session, scope } = surface(id)
    const start = session.startDesktop()
    await flushPromises()
    scope.stop()
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS)
    resolveStatus(status(id, 'held'))
    await start
    await flushPromises()

    expect(api.startDesktop).toHaveBeenCalledOnce()
    expect(api.stopDesktop).toHaveBeenCalledOnce()
    expect(desktopViewerSession(id).wanted).toBe(false)
    expect(desktopViewerSession(id).leaseState).not.toBe('held')
  })

  it('uses the latest reconciled status after recovery reacquires a new revision', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    const store = useDesktopStore()
    await session.startDesktop()
    vi.mocked(api.getDesktopStatus)
      .mockImplementationOnce(async () => ({
        ...status(id, 'expired'),
        active: false,
        proxy_url: null,
        revision: desktopViewerSession(id).revision,
      }))
      .mockImplementationOnce(async () => ({
        ...status(id, 'held', 'epoch-1'),
        revision: desktopViewerSession(id).revision,
      }))

    await session.handleReconnect()

    expect(api.startDesktop).toHaveBeenCalledTimes(2)
    expect(desktopViewerSession(id).revision).toBe(2)
    expect(desktopViewerSession(id).leaseState).toBe('held')
    expect(store.viewerGeneration).toBe(1)
  })

  it('awaits status reconciliation before manually remounting a connected viewer', async () => {
    const id = `viewer-${++count}`
    const { session } = surface(id)
    const store = useDesktopStore()
    await session.startDesktop()
    let resolveStatus!: (value: ReturnType<typeof status>) => void
    vi.mocked(api.getDesktopStatus).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStatus = resolve
      }),
    )

    const reconnect = session.handleReconnect()
    expect(reconnect).toBeInstanceOf(Promise)
    await flushPromises()
    expect(store.viewerGeneration).toBe(0)

    resolveStatus(status(id))
    await reconnect
    expect(store.viewerGeneration).toBe(1)
    expect(api.startDesktop).toHaveBeenCalledOnce()
  })

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
