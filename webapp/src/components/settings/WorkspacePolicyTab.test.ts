import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import WorkspacePolicyTab from './WorkspacePolicyTab.vue'
import * as organizationsApi from '@/services/organizations.api'
import { useAuthStore } from '@/stores/auth'

vi.mock('@/services/organizations.api', () => ({
  getOrganization: vi.fn(),
  updateOrganizationWorkspacePolicy: vi.fn(),
}))
vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))
enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())
const organization = {
  id: 'org-1',
  name: 'Acme',
  slug: 'acme',
  role: 'admin' as const,
  workspace_auto_stop_timeout_minutes: null as number | null,
  image_versions_to_keep: 2,
  created_at: '2026-01-01T00:00:00.000Z',
}
const getOrganizationMock = vi.mocked(organizationsApi.getOrganization)
const updatePolicyMock = vi.mocked(organizationsApi.updateOrganizationWorkspacePolicy)
async function autosave() {
  await vi.advanceTimersByTimeAsync(500)
  await flushPromises()
}

describe('WorkspacePolicyTab', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useAuthStore().activeOrganizationId = 'org-1'
    useAuthStore().organizations = [{ ...organization }]
    vi.useFakeTimers()
    vi.clearAllMocks()
    getOrganizationMock.mockResolvedValue({ ...organization })
    updatePolicyMock.mockImplementation(async (_id, payload) => ({ ...organization, ...payload }))
  })

  it('loads without a write or Save button and autosaves the policy switch', async () => {
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()
    await autosave()
    expect(updatePolicyMock).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Save Policy')
    expect(wrapper.get('#workspace-auto-stop').attributes('aria-checked')).toBe('false')
    await wrapper.get('#workspace-auto-stop').trigger('click')
    await autosave()
    expect(updatePolicyMock).toHaveBeenCalledWith('org-1', {
      workspace_auto_stop_timeout_minutes: 240,
      image_versions_to_keep: 2,
    })
    expect(wrapper.get('[data-testid="workspace-policy-status"]').text()).toBe('All changes saved')
  })

  it('keeps blank and invalid input local until a valid timeout is entered', async () => {
    getOrganizationMock.mockResolvedValue({
      ...organization,
      workspace_auto_stop_timeout_minutes: 60,
    })
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()
    for (const value of ['', '0', '1.5', '10081']) {
      await wrapper.get('#auto-stop-minutes').setValue(value)
      await autosave()
      expect(updatePolicyMock).not.toHaveBeenCalled()
      expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    }
    await wrapper.get('#auto-stop-minutes').setValue('120')
    await autosave()
    expect(updatePolicyMock).toHaveBeenCalledWith('org-1', {
      workspace_auto_stop_timeout_minutes: 120,
      image_versions_to_keep: 2,
    })
  })

  it('validates retention without rewriting partial input and saves the corrected count', async () => {
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()
    const input = wrapper.get('#image-versions-to-keep')
    for (const value of ['', '0', '21', '1.5']) {
      await input.setValue(value)
      await autosave()
      expect(updatePolicyMock).not.toHaveBeenCalled()
    }
    await input.setValue('5')
    await autosave()
    expect(updatePolicyMock).toHaveBeenCalledWith('org-1', {
      workspace_auto_stop_timeout_minutes: null,
      image_versions_to_keep: 5,
    })
  })

  it('retains the form and input after an error and retries in place', async () => {
    updatePolicyMock.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()
    await wrapper.get('#image-versions-to-keep').setValue('4')
    await autosave()
    expect(wrapper.get('input').element.value).toBe('4')
    expect(wrapper.text()).toContain('offline')
    await wrapper.get('[data-testid="workspace-policy-status"] button').trigger('click')
    await flushPromises()
    expect(updatePolicyMock).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('All changes saved')
  })
  it('shows the policy as read-only to organization members', async () => {
    useAuthStore().organizations[0]!.role = 'member'
    const wrapper = mount(WorkspacePolicyTab)
    await flushPromises()
    expect(wrapper.get('#workspace-auto-stop').attributes('disabled')).toBeDefined()
    expect(wrapper.get('#image-versions-to-keep').attributes('disabled')).toBeDefined()
    await autosave()
    expect(updatePolicyMock).not.toHaveBeenCalled()
  })
})
