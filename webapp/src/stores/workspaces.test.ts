import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useWorkspaceStore } from './workspaces'
import {
  WorkspaceOperation,
  WorkspaceStatus,
  RuntimeType,
  type Workspace,
} from '@/types'
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
    credentials_present: overrides.credentials_present ?? false,
  }
}

describe('workspace transition state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
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
      credentials_present: true,
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
      credentials_present: false,
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
