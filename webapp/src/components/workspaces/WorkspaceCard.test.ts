import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import WorkspaceCard from './WorkspaceCard.vue'
import { WorkspaceStatus, type Workspace } from '@/types'

const store = vi.hoisted(() => ({ getWorkspaceTransitionLabel: vi.fn() }))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => store }))

const windows = {
  id: 'ws',
  name: 'Windows',
  status: WorkspaceStatus.STOPPED,
  runner_online: true,
  guest_os: 'windows',
  windows_license: { kind: 'device_key', activation_status: 'pending', activation_code: '' },
} as Workspace

function render(patch: Partial<Workspace> = {}) {
  return mount(WorkspaceCard, {
    props: { workspace: { ...windows, ...patch } },
    global: { stubs: { WorkspaceActions: true, ImageVersionBadge: true } },
  })
}

const statusBadge = (wrapper: ReturnType<typeof render>) =>
  wrapper.get('[data-slot="badge"]:not([data-testid="workspace-guest-badge"])')

describe('WorkspaceCard', () => {
  beforeEach(() => store.getWorkspaceTransitionLabel.mockReturnValue(''))

  it('keeps the status whole and lets the license badge truncate', () => {
    const wrapper = render()
    expect(statusBadge(wrapper).text()).toBe('Stopped')
    expect(statusBadge(wrapper).classes()).toContain('shrink-0')
    const guest = wrapper.get('[data-testid="workspace-guest-badge"]')
    expect(guest.text()).toBe('Activation pending')
    expect(guest.attributes('title')).toBe('Windows · Activation pending')
    expect(guest.classes()).toContain('shrink')
    expect(guest.classes()).not.toContain('shrink-0')
  })

  it('shows only the status while the workspace is in transition', () => {
    store.getWorkspaceTransitionLabel.mockReturnValue('Creating…')
    const wrapper = render({ status: WorkspaceStatus.CREATING })
    expect(statusBadge(wrapper).text()).toBe('Creating…')
    expect(wrapper.find('[data-testid="workspace-guest-badge"]').exists()).toBe(false)
  })
})
