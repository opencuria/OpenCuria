import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import EditCredentialDialog from './EditCredentialDialog.vue'
import type { Credential } from '@/types'

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())

const credentialStore = vi.hoisted(() => ({ updateCredential: vi.fn() }))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentialStore }))

const credential: Credential = {
  id: 'oauth-1',
  name: 'Notion Work',
  scope: 'personal',
  service_id: 'svc-oauth',
  service_name: 'Notion OAuth',
  service_slug: 'notion-oauth',
  credential_type: 'mcp_oauth',
  env_var_name: '',
  target_path: '',
  has_public_key: false,
  created_by_id: 1,
  created_at: '2026-01-01',
  updated_at: '2026-01-01',
  oauth_connected: true,
  oauth_status: 'connected',
  oauth_reconnect_required: false,
  oauth_expires_at: null,
}
const stubs = {
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<h2><slot /></h2>' },
  DialogDescription: { template: '<p><slot /></p>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}
async function mountDialog(values: Partial<Credential> = {}) {
  const wrapper = mount(EditCredentialDialog, {
    props: { credential: null },
    global: { stubs },
  })
  await wrapper.setProps({ credential: { ...credential, ...values } })
  return wrapper
}

describe('EditCredentialDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.clearAllMocks()
    credentialStore.updateCredential.mockResolvedValue(true)
  })

  it('uses the common edit dialog for OAuth names without token fields or extra explanations', async () => {
    const wrapper = await mountDialog()
    expect(wrapper.find('h2').text()).toBe('Edit Credential')
    expect(wrapper.text()).toContain('Update the credential name.')
    expect(wrapper.text()).not.toMatch(/token|provider-managed/i)
    expect(wrapper.findAll('input')).toHaveLength(1)
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.find('textarea').exists()).toBe(false)
    await wrapper.find('input').setValue('Notion Team')
    await wrapper.find('#edit-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.updateCredential).toHaveBeenCalledWith('oauth-1', {
      name: 'Notion Team',
    })
  })

  it('retains value replacement for ordinary environment credentials', async () => {
    const wrapper = await mountDialog({ credential_type: 'env', env_var_name: 'GITHUB_TOKEN' })
    expect(wrapper.text()).toContain('Update the credential name or replace its value.')
    await wrapper.find('input[type="password"]').setValue('replacement-secret')
    await wrapper.find('#edit-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.updateCredential).toHaveBeenCalledWith('oauth-1', {
      value: 'replacement-secret',
    })
  })

  it('does not update an unchanged OAuth name', async () => {
    const wrapper = await mountDialog()
    await wrapper.find('#edit-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.updateCredential).not.toHaveBeenCalled()
  })
  it('discards edited names and secrets on Cancel without any automatic writes', async () => {
    const wrapper = await mountDialog({ credential_type: 'env', env_var_name: 'TOKEN' })
    await wrapper.findAll('input')[0]!.setValue('Draft name')
    await wrapper.get('input[type="password"]').setValue('draft-secret')
    await vi.advanceTimersByTimeAsync(1000)
    expect(credentialStore.updateCredential).not.toHaveBeenCalled()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Cancel')!
      .trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
    expect(credentialStore.updateCredential).not.toHaveBeenCalled()
  })

  it('keeps failed changes visible and closes only after a successful Save', async () => {
    credentialStore.updateCredential.mockResolvedValueOnce(false)
    const wrapper = await mountDialog()
    await wrapper.get('input').setValue('Updated name')
    await wrapper.get('#edit-credential-form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('Failed to update')
    expect(wrapper.emitted('close')).toBeUndefined()
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('Updated name')
    await wrapper.get('#edit-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.updateCredential).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('does not submit a blank credential name', async () => {
    const wrapper = await mountDialog()
    await wrapper.get('input').setValue('')
    await wrapper.get('#edit-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.updateCredential).not.toHaveBeenCalled()
  })
})
