import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspacePicker from './WorkspacePicker.vue'
import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'

const workspaceStore = {
  workspaces: [] as Workspace[],
  fetchWorkspaces: vi.fn(),
  getWorkspaceTransitionLabel: vi.fn().mockReturnValue('Starting…'),
}

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

function mountPicker(props: Record<string, unknown> = {}) {
  setActivePinia(createPinia())
  return mount(WorkspacePicker, {
    props: { modelValue: null, ...props },
    global: {
      stubs: {
        CreateWorkspaceDialog: { template: '<div />' },
        DropdownMenu: { template: '<div><slot /></div>' },
        DropdownMenuTrigger: { template: '<div><slot /></div>' },
        DropdownMenuContent: { template: '<div><slot /></div>' },
        DropdownMenuItem: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
        DropdownMenuSeparator: true,
        Input: { template: '<input />' },
      },
    },
  })
}

function runningWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'ws-1',
    runner_id: 'runner-1',
    name: 'Alpha',
    status: WorkspaceStatus.RUNNING,
    runner_online: true,
    active_operation: null,
    runtime_type: 'docker',
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    last_activity_at: '2026-03-29T10:00:00.000Z',
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    created_at: '2026-03-29T10:00:00.000Z',
    updated_at: '2026-03-29T10:00:00.000Z',
    has_active_session: false,
    credential_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

describe('WorkspacePicker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the selected workspace name with a ready status pill', () => {
    workspaceStore.workspaces = [runningWorkspace()]
    const wrapper = mountPicker({ modelValue: 'ws-1' })

    const trigger = wrapper.get('[data-testid="workspace-picker-trigger"]')
    expect(trigger.text()).toContain('Alpha')
    expect(trigger.classes().join(' ')).not.toContain('border-amber-500/40')
  })

  it('uses an amber pill when no ready workspace is selected', () => {
    workspaceStore.workspaces = [
      runningWorkspace({ id: 'ws-1', status: WorkspaceStatus.STOPPED, runner_online: false }),
    ]
    const wrapper = mountPicker({ modelValue: 'ws-1' })

    const trigger = wrapper.get('[data-testid="workspace-picker-trigger"]')
    expect(trigger.classes().join(' ')).toContain('border-amber-500/40')
  })

  it('emits update:modelValue when an option is clicked', async () => {
    workspaceStore.workspaces = [
      runningWorkspace(),
      runningWorkspace({ id: 'ws-2', name: 'Beta' }),
    ]
    const wrapper = mountPicker({ modelValue: 'ws-1' })

    const option = wrapper
      .findAll('[data-testid="workspace-picker-option"]')
      .find((item) => item.text().includes('Beta'))
    expect(option).toBeTruthy()
    await option!.trigger('click')

    expect(wrapper.emitted('update:modelValue')).toEqual([['ws-2']])
  })
})
