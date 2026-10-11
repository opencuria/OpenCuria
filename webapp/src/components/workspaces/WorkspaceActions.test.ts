import { shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import WorkspaceActions from './WorkspaceActions.vue'
import { RuntimeType, WorkspaceStatus, type Workspace } from '@/types'

vi.mock('@/stores/workspaces', () => ({
  useWorkspaceStore: () => ({
    isWorkspaceTransitioning: () => false,
    getWorkspaceTransitionLabel: () => '',
    stopWorkspace: vi.fn(),
    resumeWorkspace: vi.fn(),
    removeWorkspace: vi.fn(),
  }),
}))
const auth = vi.hoisted(() => ({ isAdmin: true }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))

vi.mock('./EditWorkspaceDialog.vue', () => ({
  default: {
    name: 'EditWorkspaceDialog',
    template: '<div />',
  },
}))

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'workspace-1',
    runner_id: 'runner-1',
    status: WorkspaceStatus.RUNNING,
    active_operation: null,
    name: 'Workspace',
    runtime_type: RuntimeType.QEMU,
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    last_activity_at: '2026-04-01T10:00:00.000Z',
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    created_at: '2026-04-01T10:00:00.000Z',
    updated_at: '2026-04-01T10:00:00.000Z',
    has_active_session: false,
    runner_online: true,
    credential_ids: [],
    plugin_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

describe('WorkspaceActions capture gating', () => {
  it('links admins directly to runner activity while keeping lifecycle actions blocked', async () => {
    const event = vi.fn()
    window.addEventListener('opencuria:open-settings', event)
    const wrapper = shallowMount(WorkspaceActions, {
      props: { workspace: makeWorkspace({ intervention_required: true }) },
      global: { stubs: { Button: { name: 'Button', template: '<button><slot /></button>' } } },
    })
    const buttons = wrapper.findAllComponents({ name: 'Button' })
    const review = buttons.find((button) => button.text() === 'Review workspace issue')!
    expect(review).toBeTruthy()
    await review.trigger('click')
    expect(event.mock.calls[0]![0].detail).toEqual({
      tab: 'runners',
      runnerId: 'runner-1',
      runnerActivity: true,
    })
    const capture = buttons.find((button) => button.attributes('title') === 'Capture image')!
    expect(['true', '']).toContain(capture.attributes('disabled'))
    window.removeEventListener('opencuria:open-settings', event)
    wrapper.unmount()
  })
  it('explains the admin requirement to a member without offering an inaccessible link', () => {
    auth.isAdmin = false
    const wrapper = shallowMount(WorkspaceActions, {
      props: { workspace: makeWorkspace({ intervention_required: true }) },
    })
    expect(wrapper.text()).toContain('Ask an organization admin')
    expect(wrapper.text()).not.toContain('Review workspace issue')
    wrapper.unmount()
    auth.isAdmin = true
  })
  it('enables capture for a stopped QEMU workspace without credentials on disk', () => {
    const wrapper = shallowMount(WorkspaceActions, {
      props: {
        workspace: makeWorkspace({
          status: WorkspaceStatus.STOPPED,
          credentials_present: false,
        }),
      },
    })

    const capture = wrapper.findAllComponents({ name: 'Button' }).find((button) => {
      return button.attributes('title') === 'Capture image'
    })
    expect(capture).toBeTruthy()
    expect(capture?.attributes('disabled')).not.toBe('true')
    expect(capture?.attributes('disabled')).not.toBe('')
  })

  it('disables capture when credentials are still on disk', () => {
    const wrapper = shallowMount(WorkspaceActions, {
      props: {
        workspace: makeWorkspace({
          status: WorkspaceStatus.STOPPED,
          credentials_present: true,
        }),
      },
    })

    const capture = wrapper.findAllComponents({ name: 'Button' }).find((button) => {
      return String(button.attributes('title') || '').includes('Credentials are still on disk')
    })
    expect(capture).toBeTruthy()
    expect(['true', '']).toContain(capture?.attributes('disabled'))
  })
})
