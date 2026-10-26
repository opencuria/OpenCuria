import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import WorkspaceActions from './WorkspaceActions.vue'
import { WorkspaceStatus, type Workspace } from '@/types'

const mocks = vi.hoisted(() => ({
  isWorkspaceTransitioning: vi.fn(),
  getWorkspaceTransitionLabel: vi.fn(),
  stopWorkspace: vi.fn(),
  resumeWorkspace: vi.fn(),
  removeWorkspace: vi.fn(),
  showEdit: vi.fn(),
  auth: { isAdmin: true },
}))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => mocks }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('./EditWorkspaceDialog.vue', () => ({
  default: {
    name: 'EditWorkspaceDialog',
    setup(_: unknown, { expose }: { expose: (methods: object) => void }) {
      expose({ show: mocks.showEdit })
    },
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

const mounted: ReturnType<typeof mount>[] = []
function mountActions(overrides: Partial<Workspace> = {}, hideDestructive = false) {
  const onClick = vi.fn()
  const wrapper = mount(WorkspaceActions, {
    props: { workspace: makeWorkspace(overrides), hideDestructive, onClick },
    attachTo: document.body,
  })
  mounted.push(wrapper)
  return { wrapper, onClick }
}
async function openMenu(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('button[title="More workspace actions"]').trigger('keydown', { key: 'Enter' })
  await flushPromises()
}
function menuItem(label: string) {
  const element = [...document.querySelectorAll('[role="menuitem"]')].find(
    (item) => item.textContent?.trim() === label,
  )
  expect(element, `Menu item: ${label}`).toBeTruthy()
  return new DOMWrapper(element!)
}

describe('WorkspaceActions menu', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.auth.isAdmin = true
    mocks.isWorkspaceTransitioning.mockReturnValue(false)
    mocks.getWorkspaceTransitionLabel.mockReturnValue('')
  })
  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.restoreAllMocks()
  })

  it('keeps stop and the menu visible and opens editing without clicking through the card', async () => {
    const { wrapper, onClick } = mountActions()
    expect(wrapper.findAll('button')).toHaveLength(2)
    expect(document.querySelector('[role="menuitem"]')).toBeNull()
    await wrapper.get('button[title="Stop workspace"]').trigger('click')
    expect(mocks.stopWorkspace).toHaveBeenCalledWith('workspace-1')
    await openMenu(wrapper)
    await menuItem('Edit workspace').trigger('click')
    expect(mocks.showEdit).toHaveBeenCalledOnce()
    expect(onClick).not.toHaveBeenCalled()
  })
  it('starts a stopped workspace directly', async () => {
    const { wrapper } = mountActions({ status: WorkspaceStatus.STOPPED })
    await wrapper.get('button[title="Resume workspace"]').trigger('click')
    expect(mocks.resumeWorkspace).toHaveBeenCalledWith('workspace-1')
  })
  it('emits capture and reset from the menu', async () => {
    const { wrapper, onClick } = mountActions({
      pending_base_image: { id: 'image-1', version: 1 } as Workspace['pending_base_image'],
    })
    await openMenu(wrapper)
    await menuItem('Capture image').trigger('click')
    expect(wrapper.emitted('captureImage')).toHaveLength(1)
    await openMenu(wrapper)
    await menuItem('Reset workspace').trigger('click')
    expect(wrapper.emitted('recreate')).toHaveLength(1)
    expect(onClick).not.toHaveBeenCalled()
  })
  it('requires confirmation before removing a workspace', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { wrapper } = mountActions()
    await openMenu(wrapper)
    await menuItem('Remove workspace').trigger('click')
    expect(mocks.removeWorkspace).not.toHaveBeenCalled()
    confirm.mockReturnValue(true)
    await openMenu(wrapper)
    await menuItem('Remove workspace').trigger('click')
    expect(mocks.removeWorkspace).toHaveBeenCalledWith('workspace-1')
  })
  it('blocks capture when stopped credentials remain on disk', async () => {
    const { wrapper } = mountActions({ status: WorkspaceStatus.STOPPED, credentials_present: true })
    await openMenu(wrapper)
    const capture = menuItem('Capture image')
    expect(capture.attributes('data-disabled')).toBeDefined()
    expect(capture.attributes('title')).toContain('Credentials are still on disk')
    await capture.trigger('click')
    expect(wrapper.emitted('captureImage')).toBeUndefined()
  })
  it('allows capture of a stopped workspace after credentials have been removed', async () => {
    const { wrapper } = mountActions({ status: WorkspaceStatus.STOPPED })
    await openMenu(wrapper)
    expect(menuItem('Capture image').attributes('data-disabled')).toBeUndefined()
    await menuItem('Capture image').trigger('click')
    expect(wrapper.emitted('captureImage')).toHaveLength(1)
  })
  it('blocks actions offline while keeping the menu accessible', async () => {
    const { wrapper } = mountActions({ runner_online: false })
    expect(wrapper.get('button[title="Runner offline"]').attributes('disabled')).toBeDefined()
    await openMenu(wrapper)
    expect(menuItem('Edit workspace').attributes('data-disabled')).toBeDefined()
    expect(menuItem('Remove workspace').attributes('data-disabled')).toBeDefined()
    expect(document.body.textContent).not.toContain('Capture image')
  })
  it('shows a single pending action and disables menu actions during transitions', async () => {
    mocks.isWorkspaceTransitioning.mockReturnValue(true)
    mocks.getWorkspaceTransitionLabel.mockReturnValue('Stopping')
    const { wrapper } = mountActions()
    expect(wrapper.findAll('button')).toHaveLength(2)
    expect(wrapper.get('button[title="Stopping"]').attributes('disabled')).toBeDefined()
    await openMenu(wrapper)
    expect(menuItem('Capture image').attributes('data-disabled')).toBeDefined()
    expect(menuItem('Edit workspace').attributes('data-disabled')).toBeDefined()
  })
  it('links admins to runner activity while keeping lifecycle actions blocked', async () => {
    const event = vi.fn()
    window.addEventListener('opencuria:open-settings', event)
    try {
      const { wrapper } = mountActions({ intervention_required: true })
      await openMenu(wrapper)
      expect(menuItem('Capture image').attributes('data-disabled')).toBeDefined()
      await menuItem('Review workspace issue').trigger('click')
      expect(event.mock.calls[0]![0].detail).toEqual({
        tab: 'runners',
        runnerId: 'runner-1',
        runnerActivity: true,
      })
    } finally {
      window.removeEventListener('opencuria:open-settings', event)
    }
  })
  it('explains the admin requirement without offering members an inaccessible action', async () => {
    mocks.auth.isAdmin = false
    const { wrapper } = mountActions({ intervention_required: true })
    await openMenu(wrapper)
    expect(document.body.textContent).toContain('Ask an organization admin')
    expect(document.body.textContent).not.toContain('Review workspace issue')
  })
  it('respects the hidden destructive actions setting', async () => {
    const { wrapper } = mountActions({}, true)
    expect(wrapper.find('button[title="Stop workspace"]').exists()).toBe(false)
    await openMenu(wrapper)
    expect(document.body.textContent).not.toContain('Remove workspace')
    expect(document.body.textContent).not.toContain('Reset workspace')
    expect(menuItem('Capture image').exists()).toBe(true)
  })
})
