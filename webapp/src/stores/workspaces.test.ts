import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useWorkspaceStore } from './workspaces'
import { WorkspaceOperation, WorkspaceStatus, RuntimeType, type Workspace } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { toast } from 'vue-sonner'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/services/workspaces.api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/workspaces.api')>()
  return {
    ...actual,
    updateWorkspace: vi.fn(),
    getWorkspace: vi.fn(),
    listWorkspaces: vi.fn(),
  }
})

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: overrides.id ?? 'workspace-1',
    runner_id: overrides.runner_id ?? 'runner-1',
    status: overrides.status ?? WorkspaceStatus.RUNNING,
    active_operation: overrides.active_operation ?? null,
    name: overrides.name ?? 'Workspace',
    runtime_type: overrides.runtime_type ?? RuntimeType.DOCKER,
    qemu_vcpus: overrides.qemu_vcpus ?? null,
    qemu_memory_mb: overrides.qemu_memory_mb ?? null,
    qemu_disk_size_gb: overrides.qemu_disk_size_gb ?? null,
    desktop_width: overrides.desktop_width ?? 1920,
    desktop_height: overrides.desktop_height ?? 1080,
    created_by_id: overrides.created_by_id ?? 1,
    last_activity_at: overrides.last_activity_at ?? '2026-03-29T10:00:00.000Z',
    auto_stop_timeout_minutes: overrides.auto_stop_timeout_minutes ?? null,
    auto_stop_at: overrides.auto_stop_at ?? null,
    delete_requested_at: overrides.delete_requested_at ?? null,
    delete_started_at: overrides.delete_started_at ?? null,
    delete_confirmed_at: overrides.delete_confirmed_at ?? null,
    delete_last_error: overrides.delete_last_error ?? '',
    delete_attempt_count: overrides.delete_attempt_count ?? 0,
    created_at: overrides.created_at ?? '2026-03-29T10:00:00.000Z',
    updated_at: overrides.updated_at ?? '2026-03-29T10:00:00.000Z',
    has_active_session: overrides.has_active_session ?? false,
    runner_online: overrides.runner_online ?? true,
    credential_ids: overrides.credential_ids ?? [],
    plugin_ids: overrides.plugin_ids ?? [],
    credentials_present: overrides.credentials_present ?? false,
    intervention_required: overrides.intervention_required,
    lifecycle_diagnostic: overrides.lifecycle_diagnostic,
  }
}

describe('workspace transition state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('coalesces workspace detail requests and ignores an older route response', async () => {
    const store = useWorkspaceStore()
    const first = makeWorkspace({ id: 'workspace-1', name: 'first' })
    const second = makeWorkspace({ id: 'workspace-2', name: 'second' })
    let resolveFirst!: (value: Workspace) => void
    vi.mocked(workspacesApi.getWorkspace).mockImplementation((id) =>
      id === 'workspace-1'
        ? new Promise((resolve) => {
            resolveFirst = resolve
          })
        : Promise.resolve(second as never),
    )
    const a = store.fetchWorkspaceDetail('workspace-1')
    const duplicate = store.fetchWorkspaceDetail('workspace-1')
    expect(workspacesApi.getWorkspace).toHaveBeenCalledTimes(1)
    await store.fetchWorkspaceDetail('workspace-2')
    resolveFirst(first as never)
    await Promise.all([a, duplicate])
    expect(store.activeWorkspace?.id).toBe('workspace-2')
  })

  it('derives transition labels from backend active_operation', () => {
    const store = useWorkspaceStore()
    store.workspaces = [
      makeWorkspace({
        id: 'workspace-restart',
        active_operation: WorkspaceOperation.RESTARTING,
      }),
    ]

    expect(store.isWorkspaceTransitioning('workspace-restart')).toBe(true)
    expect(store.getWorkspaceTransitionLabel('workspace-restart')).toBe('Restarting…')
  })

  it('clears optimistic pending state when the backend operation resets', () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace({ id: 'workspace-stop' })]
    store.pendingWorkspaceOperations['workspace-stop'] = {
      operation: 'stop',
      expectedStatus: WorkspaceStatus.STOPPED,
    }

    store.updateWorkspaceOperation('workspace-stop', null)

    expect(store.pendingWorkspaceOperations['workspace-stop']).toBeUndefined()
    expect(store.isWorkspaceTransitioning('workspace-stop')).toBe(false)
  })

  it('treats legacy removed workspaces as completed removals', () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace({ id: 'workspace-remove' })]
    store.pendingWorkspaceOperations['workspace-remove'] = {
      operation: 'remove',
      expectedStatus: WorkspaceStatus.DELETED,
    }

    store.updateWorkspaceStatus('workspace-remove', WorkspaceStatus.REMOVED)

    expect(store.pendingWorkspaceOperations['workspace-remove']).toBeUndefined()
    expect(store.workspacesByStatus.removed.map((workspace) => workspace.id)).toEqual([
      'workspace-remove',
    ])
    expect(store.isWorkspaceTransitioning('workspace-remove')).toBe(true)
  })
})

describe('workspace credential updates', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('applies credentials_present from PATCH and toasts a live apply on running workspaces', async () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace({ credential_ids: [], credentials_present: false })]
    vi.mocked(workspacesApi.updateWorkspace).mockResolvedValue({
      id: 'workspace-1',
      name: 'Workspace',
      updated_at: '2026-03-29T11:00:00.000Z',
      active_operation: null,
      credential_ids: ['cred-1'],
      plugin_ids: [],
      credentials_present: true,
      credential_sync_status: 'synced',
      qemu_vcpus: null,
      qemu_memory_mb: null,
      qemu_disk_size_gb: null,
      desktop_width: 1920,
      desktop_height: 1080,
    })

    const success = await store.updateWorkspace('workspace-1', {
      credential_ids: ['cred-1'],
    })

    expect(success).toBe(true)
    const workspace = store.workspaces[0]
    expect(workspace?.credential_ids).toEqual(['cred-1'])
    expect(workspace?.credentials_present).toBe(true)
    expect(toast.success).toHaveBeenCalledWith(
      'Credentials updated',
      expect.objectContaining({
        description: 'Secrets were applied to the running workspace.',
      }),
    )
  })

  it('sends plugin IDs in one workspace patch and warns when runner synchronization is pending', async () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace({ plugin_ids: [] })]
    vi.mocked(workspacesApi.updateWorkspace).mockResolvedValue({
      id: 'workspace-1',
      name: 'Workspace',
      updated_at: '2026-03-29T11:00:00.000Z',
      active_operation: null,
      credential_ids: ['cred-1'],
      plugin_ids: ['plugin-1'],
      credentials_present: false,
      credential_sync_status: 'pending',
      credential_sync_detail: 'Runner is offline.',
      qemu_vcpus: null,
      qemu_memory_mb: null,
      qemu_disk_size_gb: null,
      desktop_width: 1920,
      desktop_height: 1080,
    })
    const success = await store.updateWorkspace('workspace-1', {
      credential_ids: ['cred-1'],
      plugin_ids: ['plugin-1'],
    })
    expect(success).toBe(true)
    expect(workspacesApi.updateWorkspace).toHaveBeenCalledTimes(1)
    expect(workspacesApi.updateWorkspace).toHaveBeenCalledWith('workspace-1', {
      credential_ids: ['cred-1'],
      plugin_ids: ['plugin-1'],
    })
    expect(store.workspaces[0]?.plugin_ids).toEqual(['plugin-1'])
    expect(toast.warning).toHaveBeenCalledWith(
      'Workspace configuration saved',
      expect.objectContaining({ description: 'Runner is offline.' }),
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('toasts a deferred apply when credentials change on a stopped workspace', async () => {
    const store = useWorkspaceStore()
    store.workspaces = [
      makeWorkspace({
        status: WorkspaceStatus.STOPPED,
        credential_ids: ['cred-1'],
        credentials_present: false,
      }),
    ]
    vi.mocked(workspacesApi.updateWorkspace).mockResolvedValue({
      id: 'workspace-1',
      name: 'Workspace',
      updated_at: '2026-03-29T11:00:00.000Z',
      active_operation: null,
      credential_ids: [],
      plugin_ids: [],
      credentials_present: false,
      credential_sync_status: 'not_required',
      qemu_vcpus: null,
      qemu_memory_mb: null,
      qemu_disk_size_gb: null,
      desktop_width: 1920,
      desktop_height: 1080,
    })

    await store.updateWorkspace('workspace-1', { credential_ids: [] })

    expect(toast.success).toHaveBeenCalledWith(
      'Workspace updated',
      expect.objectContaining({
        description: 'Credentials will be applied the next time the workspace starts.',
      }),
    )
  })
})

describe('automatic capture fence', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(workspacesApi.getWorkspace).mockResolvedValue(
      makeWorkspace({ active_operation: WorkspaceOperation.CAPTURING_IMAGE }) as never,
    )
  })

  it.each([WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED])(
    'keeps Capturing through stop/resume from %s until authoritative completion',
    async (status) => {
      const store = useWorkspaceStore()
      store.workspaces = [makeWorkspace({ status })]
      let accept!: () => void
      const request = vi.fn(
        () =>
          new Promise<void>((resolve) => {
            accept = resolve
          }),
      )
      const flight = store.captureImage('workspace-1', request)
      expect(request).toHaveBeenCalledOnce()
      expect(store.isWorkspaceTransitioning('workspace-1')).toBe(true)
      expect(store.canUseWorkspace('workspace-1')).toBe(false)
      expect(store.getWorkspaceTransitionLabel('workspace-1')).toBe('Capturing')
      store.updateWorkspaceOperation('workspace-1', null) // old event during POST
      store.updateWorkspaceStatus('workspace-1', WorkspaceStatus.STOPPED)
      expect(store.isWorkspaceTransitioning('workspace-1')).toBe(true)
      store.updateWorkspaceOperation('workspace-1', WorkspaceOperation.CAPTURING_IMAGE)
      store.updateWorkspaceStatus('workspace-1', status)
      accept()
      expect(await flight).toBe(true)
      expect(store.getWorkspaceTransitionLabel('workspace-1')).toBe('Capturing')
      store.updateWorkspaceOperation('workspace-1', null)
      expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
      expect(store.canUseWorkspace('workspace-1')).toBe(status === WorkspaceStatus.RUNNING)
    },
  )

  it('rolls back failed requests and prevents duplicate capture and workspace mutations', async () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace()]
    let reject!: (error: Error) => void
    const flight = store.captureImage(
      'workspace-1',
      () =>
        new Promise((_, fail) => {
          reject = fail
        }),
    )
    const duplicate = vi.fn()
    expect(await store.captureImage('workspace-1', duplicate)).toBe(false)
    expect(duplicate).not.toHaveBeenCalled()
    expect(await store.updateWorkspace('workspace-1', { name: 'blocked' })).toBe(false)
    expect(workspacesApi.updateWorkspace).not.toHaveBeenCalled()
    reject(new Error('409 busy'))
    await expect(flight).rejects.toThrow('409 busy')
    expect(store.canUseWorkspace('workspace-1')).toBe(true)
  })

  it('ignores a pre-request REST null and reconciles a fresh REST completion after acceptance', async () => {
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace()]
    let resolveDetail!: (value: never) => void
    vi.mocked(workspacesApi.getWorkspace).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveDetail = resolve
        }),
    )
    const detail = store.fetchWorkspaceDetail('workspace-1')
    await store.captureImage('workspace-1', async () => {})
    resolveDetail(makeWorkspace() as never)
    await detail
    expect(store.isWorkspaceTransitioning('workspace-1')).toBe(true)
    vi.mocked(workspacesApi.getWorkspace).mockResolvedValue(makeWorkspace() as never)
    await store.fetchWorkspaceDetail('workspace-1')
    expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
  })
})

it.each(['list', 'detail'])(
  'preserves newer operation events during deferred %s responses',
  async (kind) => {
    setActivePinia(createPinia())
    const store = useWorkspaceStore()
    store.workspaces = [makeWorkspace()]
    for (const operation of [WorkspaceOperation.CAPTURING_IMAGE, null]) {
      let resolve!: () => void
      const stale = makeWorkspace({
        active_operation: operation === null ? WorkspaceOperation.CAPTURING_IMAGE : null,
      })
      if (kind === 'list') {
        vi.mocked(workspacesApi.listWorkspaces).mockImplementationOnce(
          () =>
            new Promise((done) => {
              resolve = () => done([stale])
            }),
        )
      } else {
        vi.mocked(workspacesApi.getWorkspace).mockImplementationOnce(
          () =>
            new Promise((done) => {
              resolve = () => done(stale as never)
            }),
        )
      }
      const flight =
        kind === 'list' ? store.fetchWorkspaces() : store.fetchWorkspaceDetail('workspace-1')
      store.updateWorkspaceOperation('workspace-1', operation)
      resolve()
      await flight
      expect(store.workspaces[0]?.active_operation).toBe(operation)
    }
  },
)

it('reconciles missed completion via a fresh fetch after acceptance, without joining older detail loads', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace()]
  let resolve!: () => void
  vi.mocked(workspacesApi.getWorkspace)
    .mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = () => done(makeWorkspace() as never)
        }),
    )
    .mockResolvedValueOnce(makeWorkspace() as never)
  const old = store.fetchWorkspaceDetail('workspace-1')
  await store.captureImage('workspace-1', async () => {})
  expect(workspacesApi.getWorkspace).toHaveBeenCalledTimes(2)
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
  resolve()
  await old
})

it('resets completed on a newer capture while acceptance is pending', async () => {
  setActivePinia(createPinia())
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace()]
  let accept!: () => void
  vi.mocked(workspacesApi.getWorkspace).mockRejectedValueOnce(new Error('offline'))
  const flight = store.captureImage(
    'workspace-1',
    () =>
      new Promise<void>((done) => {
        accept = done
      }),
  )
  store.updateWorkspaceOperation('workspace-1', WorkspaceOperation.CAPTURING_IMAGE)
  store.updateWorkspaceOperation('workspace-1', null)
  store.updateWorkspaceOperation('workspace-1', WorkspaceOperation.CAPTURING_IMAGE)
  expect(store.pendingCaptures['workspace-1']?.completed).toBe(false)
  accept()
  await flight
  expect(store.pendingCaptures['workspace-1']).toBeDefined()
  store.updateWorkspaceOperation('workspace-1', null)
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
})

it('releases Capturing on unknown completion but preserves intervention until explicitly cleared', async () => {
  setActivePinia(createPinia())
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace()]
  vi.mocked(workspacesApi.getWorkspace).mockResolvedValue(
    makeWorkspace({ active_operation: WorkspaceOperation.CAPTURING_IMAGE }) as never,
  )
  await store.captureImage('workspace-1', async () => {})
  store.updateWorkspaceOperation('workspace-1', null, true, 'Capture outcome unknown')
  expect(store.getWorkspaceTransitionLabel('workspace-1')).toBe('Needs intervention')
  expect(store.pendingCaptures['workspace-1']).toBeUndefined()
  expect(store.canUseWorkspace('workspace-1')).toBe(false)
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(true)
  expect(await store.captureImage('workspace-1', async () => {})).toBe(false)
  expect(await store.updateWorkspace('workspace-1', { name: 'unsafe' })).toBe(false)
  store.handleWorkspaceError('workspace-1', 'Capture failed')
  store.updateWorkspaceOperation('workspace-1', null)
  expect(store.workspaces[0]?.intervention_required).toBe(true)
  expect(store.workspaces[0]?.lifecycle_diagnostic).toBe('Capture outcome unknown')
  expect(store.canUseWorkspace('workspace-1')).toBe(false)
  store.updateWorkspaceOperation('workspace-1', null, false, '')
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
  expect(store.canUseWorkspace('workspace-1')).toBe(true)
  expect(store.getWorkspaceTransitionLabel('workspace-1')).toBeNull()
})

it('restores persisted intervention and preserves newer intervention flags during deferred REST', async () => {
  setActivePinia(createPinia())
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace({ intervention_required: true })]
  expect(store.canUseWorkspace('workspace-1')).toBe(false)
  let resolve!: () => void
  vi.mocked(workspacesApi.getWorkspace).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = () => done(makeWorkspace({ intervention_required: false }) as never)
      }),
  )
  const flight = store.fetchWorkspaceDetail('workspace-1')
  store.updateWorkspaceOperation('workspace-1', null, true, 'Unknown')
  store.handleWorkspaceError('workspace-1', 'Failed')
  resolve()
  await flight
  expect(store.activeWorkspace?.intervention_required).toBe(true)
  expect(store.activeWorkspace?.lifecycle_diagnostic).toBe('Unknown')
  expect(store.canUseWorkspace('workspace-1')).toBe(false)
})

it('keeps an observed capture reserved after a child error until authoritative completion', async () => {
  setActivePinia(createPinia())
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace()]
  store.updateWorkspaceOperation('workspace-1', WorkspaceOperation.CAPTURING_IMAGE)
  store.handleWorkspaceError('workspace-1', 'Resume failed')
  expect(store.getWorkspaceTransitionLabel('workspace-1')).toBe('Capturing')
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(true)
  expect(store.canUseWorkspace('workspace-1')).toBe(false)
  expect(await store.updateWorkspace('workspace-1', { name: 'unsafe' })).toBe(false)
  expect(await store.captureImage('workspace-1', async () => {})).toBe(false)
  store.updateWorkspaceOperation('workspace-1', null, false, '')
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
  expect(store.canUseWorkspace('workspace-1')).toBe(true)
  store.handleWorkspaceError('workspace-1', 'Definitive capture failure')
  expect(store.getWorkspaceTransitionLabel('workspace-1')).toBeNull()
})

it('preserves pre-acceptance capture on error but clears the optimistic lock on API rejection', async () => {
  setActivePinia(createPinia())
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace()]
  let reject!: (error: Error) => void
  const flight = store.captureImage(
    'workspace-1',
    () =>
      new Promise((_, fail) => {
        reject = fail
      }),
  )
  store.handleWorkspaceError('workspace-1', 'Child failed')
  expect(store.pendingCaptures['workspace-1']?.requesting).toBe(true)
  expect(store.pendingCaptures['workspace-1']?.completed).toBe(false)
  expect(store.getWorkspaceTransitionLabel('workspace-1')).toBe('Capturing')
  expect(store.canUseWorkspace('workspace-1')).toBe(false)
  reject(new Error('Capture refused'))
  await expect(flight).rejects.toThrow('Capture refused')
  expect(store.pendingCaptures['workspace-1']).toBeUndefined()
  expect(store.isWorkspaceTransitioning('workspace-1')).toBe(false)
})

it('preserves legacy noncapture error cleanup', () => {
  setActivePinia(createPinia())
  const store = useWorkspaceStore()
  store.workspaces = [makeWorkspace({ active_operation: WorkspaceOperation.STARTING })]
  store.pendingWorkspaceOperations['workspace-1'] = {
    operation: 'start',
    expectedStatus: WorkspaceStatus.RUNNING,
  }
  store.handleWorkspaceError('workspace-1', 'Start failed')
  expect(store.workspaces[0]?.active_operation).toBeNull()
  expect(store.pendingWorkspaceOperations['workspace-1']).toBeUndefined()
})
