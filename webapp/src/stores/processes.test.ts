import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useProcessesStore } from './processes'
import { ProcessStatus, type WorkspaceProcess } from '@/types'
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
    listProcesses: vi.fn(),
    stopProcess: vi.fn(),
  }
})

function makeProcess(overrides: Partial<WorkspaceProcess> = {}): WorkspaceProcess {
  return {
    id: overrides.id ?? 'process-1',
    workspace_id: overrides.workspace_id ?? 'workspace-1',
    name: overrides.name ?? 'dev-server',
    command: overrides.command ?? 'npm run dev',
    workdir: overrides.workdir ?? '/workspace',
    pid: overrides.pid ?? 4242,
    log_path: overrides.log_path ?? '.opencuria/processes/process-1.log',
    status: overrides.status ?? ProcessStatus.RUNNING,
    exit_code: overrides.exit_code ?? null,
    started_at: overrides.started_at ?? '2026-09-06T10:00:00.000Z',
    ended_at: overrides.ended_at ?? null,
    updated_at: overrides.updated_at ?? '2026-09-06T10:00:00.000Z',
  }
}

describe('processes store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetches processes into per-workspace state', async () => {
    const store = useProcessesStore()
    const processes = [makeProcess(), makeProcess({ id: 'process-2', name: '' })]
    vi.mocked(workspacesApi.listProcesses).mockResolvedValue(processes)

    await store.fetchProcesses('workspace-1')

    expect(workspacesApi.listProcesses).toHaveBeenCalledWith('workspace-1')
    expect(store.processesFor('workspace-1')).toEqual(processes)
    expect(store.isLoading('workspace-1')).toBe(false)
    expect(store.errorFor('workspace-1')).toBeNull()
    expect(store.runningCountFor('workspace-1')).toBe(2)
  })

  it('records fetch errors without throwing', async () => {
    const store = useProcessesStore()
    vi.mocked(workspacesApi.listProcesses).mockRejectedValue(new Error('offline'))

    await store.fetchProcesses('workspace-1')

    expect(store.processesFor('workspace-1')).toEqual([])
    expect(store.errorFor('workspace-1')).toBe('offline')
    expect(store.isLoading('workspace-1')).toBe(false)
  })

  it('stops a process and upserts the returned record', async () => {
    const store = useProcessesStore()
    const running = makeProcess()
    store.processesByWorkspace['workspace-1'] = [running]
    const stopped = makeProcess({ status: ProcessStatus.KILLED, exit_code: 143 })
    vi.mocked(workspacesApi.stopProcess).mockResolvedValue(stopped)

    const ok = await store.stopProcess('workspace-1', 'process-1', false)

    expect(ok).toBe(true)
    expect(workspacesApi.stopProcess).toHaveBeenCalledWith('workspace-1', 'process-1', false)
    expect(store.processesFor('workspace-1')[0]?.status).toBe(ProcessStatus.KILLED)
    expect(store.isStopping('process-1')).toBe(false)
    expect(toast.success).toHaveBeenCalledWith(
      'Process stopped',
      expect.objectContaining({ description: expect.any(String) }),
    )
  })

  it('passes force through and notifies on stop failure', async () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    vi.mocked(workspacesApi.stopProcess).mockRejectedValue(new Error('boom'))

    const ok = await store.stopProcess('workspace-1', 'process-1', true)

    expect(ok).toBe(false)
    expect(workspacesApi.stopProcess).toHaveBeenCalledWith('workspace-1', 'process-1', true)
    // Failed stop keeps the local record untouched.
    expect(store.processesFor('workspace-1')[0]?.status).toBe(ProcessStatus.RUNNING)
    expect(toast.error).toHaveBeenCalledWith(
      'Stop failed',
      expect.objectContaining({ description: 'boom' }),
    )
  })

  it('applies status_changed events to the matching entry', async () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    vi.mocked(workspacesApi.listProcesses).mockResolvedValue([])

    await store.handleStatusChanged({
      workspace_id: 'workspace-1',
      process_id: 'process-1',
      status: ProcessStatus.EXITED,
      exit_code: 0,
      pid: 4242,
    })

    const updated = store.processesFor('workspace-1')[0]
    expect(updated?.status).toBe(ProcessStatus.EXITED)
    expect(updated?.exit_code).toBe(0)
    expect(store.runningCountFor('workspace-1')).toBe(0)
    // Known process: no refetch needed.
    expect(workspacesApi.listProcesses).not.toHaveBeenCalled()
  })

  it('refetches when an unknown running process appears via socket', async () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    const fresh = [makeProcess(), makeProcess({ id: 'process-9' })]
    vi.mocked(workspacesApi.listProcesses).mockResolvedValue(fresh)

    await store.handleStatusChanged({
      workspace_id: 'workspace-1',
      process_id: 'process-9',
      status: ProcessStatus.RUNNING,
      exit_code: null,
      pid: 9999,
    })

    expect(workspacesApi.listProcesses).toHaveBeenCalledWith('workspace-1')
    expect(store.processesFor('workspace-1')).toEqual(fresh)
  })

  it('ignores events for workspaces without loaded state', async () => {
    const store = useProcessesStore()
    vi.mocked(workspacesApi.listProcesses).mockResolvedValue([])

    await store.handleStatusChanged({
      workspace_id: 'workspace-unknown',
      process_id: 'process-1',
      status: ProcessStatus.EXITED,
      exit_code: 1,
      pid: null,
    })

    expect(workspacesApi.listProcesses).not.toHaveBeenCalled()
  })

  it('clears and resets state', () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    store.clearWorkspace('workspace-1')
    expect(store.processesFor('workspace-1')).toEqual([])

    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    store.reset()
    expect(store.processesByWorkspace).toEqual({})
  })
})
