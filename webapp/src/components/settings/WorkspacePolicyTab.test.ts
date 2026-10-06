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
      image_versions_to_keep: 2,
      created_at: '2026-01-01T00:00:00.000Z',
    })
    updatePolicyMock.mockResolvedValue({
      id: 'org-1',
      name: 'Acme',
      slug: 'acme',
      role: 'admin',
      workspace_auto_stop_timeout_minutes: 240,
      image_versions_to_keep: 2,
      created_at: '2026-01-01T00:00:00.000Z',
    })
  })

  it('renders the policy switch and enables auto-stop from the disabled state', async () => {
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()

    expect(wrapper.text()).toContain('Workspace Policy')
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
      image_versions_to_keep: 2,
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
      image_versions_to_keep: 2,
    })
  })

  it('saves the number of image versions to keep, clamped to at least one', async () => {
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()

    const input = wrapper.get('[data-testid="image-versions-to-keep"]')
    expect((input.element as HTMLInputElement).value).toBe('2')
    await input.setValue('0')
    expect((input.element as HTMLInputElement).value).toBe('1')
    await input.setValue('5')
    const save = wrapper.findAll('button').find((b) => b.text().includes('Save Policy'))
    await save!.trigger('click')
    await flushPromises()

    expect(updatePolicyMock).toHaveBeenCalledWith('org-1', {
      workspace_auto_stop_timeout_minutes: null,
      image_versions_to_keep: 5,
    })
  })
})
