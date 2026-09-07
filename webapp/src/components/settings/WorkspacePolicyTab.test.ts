import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import WorkspacePolicyTab from './WorkspacePolicyTab.vue'
import * as organizationsApi from '@/services/organizations.api'

vi.mock('@/services/organizations.api', () => ({
  getOrganization: vi.fn(),
  updateOrganizationWorkspacePolicy: vi.fn(),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    activeOrganizationId: 'org-1',
  }),
}))

const getOrganizationMock = vi.mocked(organizationsApi.getOrganization)
const updatePolicyMock = vi.mocked(organizationsApi.updateOrganizationWorkspacePolicy)

describe('WorkspacePolicyTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getOrganizationMock.mockResolvedValue({
      id: 'org-1',
      name: 'Acme',
      slug: 'acme',
      role: 'admin',
      workspace_auto_stop_timeout_minutes: null,
      created_at: '2026-01-01T00:00:00.000Z',
    })
    updatePolicyMock.mockResolvedValue({
      id: 'org-1',
      name: 'Acme',
      slug: 'acme',
      role: 'admin',
      workspace_auto_stop_timeout_minutes: 240,
      created_at: '2026-01-01T00:00:00.000Z',
    })
  })

  it('renders the policy switch and enables auto-stop from the disabled state', async () => {
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()

    expect(wrapper.text()).toContain('Automatic Workspace Stop')
    const toggle = wrapper.get('#workspace-auto-stop')
    expect(toggle.attributes('aria-checked')).toBe('false')

    await toggle.trigger('click')
    await flushPromises()

    expect(wrapper.get('#workspace-auto-stop').attributes('aria-checked')).toBe('true')
    expect(wrapper.get('#auto-stop-minutes').element).toBeTruthy()
  })

  it('saves the enabled timeout', async () => {
    getOrganizationMock.mockResolvedValue({
      id: 'org-1',
      name: 'Acme',
      slug: 'acme',
      role: 'admin',
      workspace_auto_stop_timeout_minutes: 60,
      created_at: '2026-01-01T00:00:00.000Z',
    })

    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()

    await wrapper.get('#auto-stop-minutes').setValue('120')
    const save = wrapper.findAll('button').find((b) => b.text().includes('Save Policy'))
    expect(save).toBeTruthy()
    await save!.trigger('click')
    await flushPromises()

    expect(updatePolicyMock).toHaveBeenCalledWith('org-1', {
      workspace_auto_stop_timeout_minutes: 120,
    })
  })
})
