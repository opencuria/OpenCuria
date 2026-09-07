import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

import HarnessProcessSheet from './HarnessProcessSheet.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useProcessesStore } from '@/stores/processes'
import { ProcessStatus, type WorkspaceProcess } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'

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
    startProcess: vi.fn(),
    restartProcess: vi.fn(),
    deleteProcess: vi.fn(),
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
    run_count: overrides.run_count ?? 1,
    started_at: overrides.started_at ?? '2026-09-06T10:00:00.000Z',
    ended_at: overrides.ended_at ?? null,
    updated_at: overrides.updated_at ?? '2026-09-06T10:00:00.000Z',
  }
}

function mountSheet() {
  return mount(HarnessProcessSheet, {
    global: {
      provide: {
        [harnessWorkspaceIdKey as symbol]: ref('workspace-1'),
      },
    },
  })
}

describe('HarnessProcessSheet', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('shows empty copy when there are no processes', () => {
    const wrapper = mountSheet()
    expect(wrapper.find('[data-testid="composer-process-empty"]').text()).toBe(
      'No background processes yet.',
    )
  })

  it('renders name, command, status, log path, and stop without force', () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    const wrapper = mountSheet()

    expect(wrapper.text()).toContain('dev-server')
    expect(wrapper.find('[data-testid="composer-process-command"]').text()).toBe('npm run dev')
    expect(wrapper.text()).toContain('running')
    expect(wrapper.find('[data-testid="composer-process-log-path"]').text()).toBe(
      '.opencuria/processes/process-1.log',
    )
    expect(wrapper.find('[data-testid="composer-process-stop"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Force')
  })

  it('hides stop for finished processes but shows start/restart/delete', () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess({ status: ProcessStatus.EXITED })]
    const wrapper = mountSheet()
    expect(wrapper.find('[data-testid="composer-process-stop"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="composer-process-start"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-process-restart"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-process-delete"]').exists()).toBe(true)
  })

  it('shows restart + stop + delete and hides start for running processes', () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess({ status: ProcessStatus.RUNNING })]
    const wrapper = mountSheet()
    expect(wrapper.find('[data-testid="composer-process-start"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="composer-process-stop"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-process-restart"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-process-delete"]').exists()).toBe(true)
  })

  it('shows the run badge only when run_count > 1', () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess({ run_count: 1 })]
    expect(mountSheet().find('[data-testid="composer-process-run"]').exists()).toBe(false)

    store.processesByWorkspace['workspace-1'] = [makeProcess({ run_count: 2 })]
    const wrapper = mountSheet()
    const badge = wrapper.find('[data-testid="composer-process-run"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('2')
  })

  it('restarts a running process', async () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    vi.mocked(workspacesApi.restartProcess).mockResolvedValue(
      makeProcess({ status: ProcessStatus.RUNNING, run_count: 2 }),
    )
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-restart"]').trigger('click')
    await flushPromises()
    expect(workspacesApi.restartProcess).toHaveBeenCalledWith('workspace-1', 'process-1')
  })

  it('starts a stopped process via the restart endpoint', async () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess({ status: ProcessStatus.EXITED })]
    vi.mocked(workspacesApi.restartProcess).mockResolvedValue(
      makeProcess({ status: ProcessStatus.RUNNING, run_count: 2 }),
    )
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-start"]').trigger('click')
    await flushPromises()
    expect(workspacesApi.restartProcess).toHaveBeenCalledWith('workspace-1', 'process-1')
  })

  it('deletes after confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    vi.mocked(workspacesApi.deleteProcess).mockResolvedValue(undefined)
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-delete"]').trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalled()
    expect(workspacesApi.deleteProcess).toHaveBeenCalledWith('workspace-1', 'process-1')
    confirmSpy.mockRestore()
  })

  it('skips delete when confirm is cancelled', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-delete"]').trigger('click')
    await flushPromises()
    expect(workspacesApi.deleteProcess).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('emits close from the header button', async () => {
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-close"]').trigger('click')
    expect(wrapper.emitted('close')).toEqual([[]])
  })

  it('refreshes via the store', async () => {
    vi.mocked(workspacesApi.listProcesses).mockResolvedValue([])
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-refresh"]').trigger('click')
    await flushPromises()
    expect(workspacesApi.listProcesses).toHaveBeenCalledWith('workspace-1')
  })

  it('stops a running process', async () => {
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    vi.mocked(workspacesApi.stopProcess).mockResolvedValue(
      makeProcess({ status: ProcessStatus.KILLED }),
    )
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-stop"]').trigger('click')
    await flushPromises()
    expect(workspacesApi.stopProcess).toHaveBeenCalledWith('workspace-1', 'process-1')
  })

  it('copies the log path', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const store = useProcessesStore()
    store.processesByWorkspace['workspace-1'] = [makeProcess()]
    const wrapper = mountSheet()
    await wrapper.find('[data-testid="composer-process-copy"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('.opencuria/processes/process-1.log')
  })
})
