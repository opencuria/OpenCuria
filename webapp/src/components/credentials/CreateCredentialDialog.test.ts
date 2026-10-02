import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import CreateCredentialDialog from './CreateCredentialDialog.vue'

const credentialStore = vi.hoisted(() => ({
  services: [] as Array<Record<string, unknown>>,
  servicesLoaded: true,
  servicesError: null as string | null,
  fetchServices: vi.fn(),
  createCredential: vi.fn(),
  connectOAuthService: vi.fn(),
}))
const auth = vi.hoisted(() => ({ isAdmin: false }))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentialStore }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))

const oauth = {
  id: 'svc-oauth',
  name: 'Notion OAuth',
  slug: 'notion-oauth',
  description: '',
  credential_type: 'mcp_oauth',
  env_var_name: '',
  target_path: '',
  label: '',
  organization_id: 'org-1',
  oauth_server_url: 'https://mcp.notion.test/mcp',
  is_active: true,
}
const env = {
  id: 'svc-env',
  name: 'GitHub',
  slug: 'github',
  description: '',
  credential_type: 'env',
  env_var_name: 'GITHUB_TOKEN',
  target_path: '',
  label: '',
  organization_id: null,
  oauth_server_url: '',
  is_active: true,
}
const stubs = {
  Dialog: { template: '<div><slot /></div>' },
  DialogTrigger: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
  Select: { template: '<div><slot /></div>' },
  SelectTrigger: { template: '<button><slot /></button>' },
  SelectValue: { template: '<span><slot /></span>' },
  SelectContent: { template: '<div><slot /></div>' },
  SelectItem: { template: '<div><slot /></div>' },
}
function mountDialog(serviceId: string) {
  return mount(CreateCredentialDialog, {
    props: { open: true, preselectedServiceId: serviceId, showTrigger: false },
    global: { stubs },
  })
}

describe('CreateCredentialDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    credentialStore.services = [oauth, env]
    credentialStore.connectOAuthService.mockResolvedValue(true)
    credentialStore.createCredential.mockResolvedValue(true)
    auth.isAdmin = false
  })

  it('starts a service-specific OAuth connect with no password/token field', async () => {
    const wrapper = mountDialog('svc-oauth')
    await flushPromises()
    expect(wrapper.text()).toContain('Connect Notion OAuth')
    expect(wrapper.text()).toContain('never asks for or displays OAuth tokens')
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.find('textarea').exists()).toBe(false)
    await wrapper.find('#create-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.connectOAuthService).toHaveBeenCalledWith('svc-oauth', undefined, false)
    expect(credentialStore.createCredential).not.toHaveBeenCalled()
  })

  it('retains ordinary environment values for env credentials', async () => {
    const wrapper = mountDialog('svc-env')
    await flushPromises()
    expect(wrapper.find('input[type="password"]').exists()).toBe(true)
    await wrapper.find('input[type="password"]').setValue('secret')
    await wrapper.find('#create-credential-form').trigger('submit')
    await flushPromises()
    expect(credentialStore.createCredential).toHaveBeenCalledWith({
      service_id: 'svc-env',
      name: undefined,
      value: 'secret',
      organization_credential: false,
    })
  })

  it('allows changing a valid preselected service, drops values across services, and retains local context after the parent consumes it', async () => {
    const wrapper = mountDialog('svc-env')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      selectedServiceId: string
      value: string
      name: string
      selectService: (id: string) => void
    }
    vm.value = 'sensitive-value'
    vm.name = 'private credential name'
    vm.selectService('svc-oauth')
    expect(vm.selectedServiceId).toBe('svc-oauth')
    expect(vm.value).toBe('')
    expect(vm.name).toBe('')
    await wrapper.setProps({ preselectedServiceId: undefined })
    expect(vm.selectedServiceId).toBe('svc-oauth')
    expect(wrapper.text()).toContain('Connect Notion OAuth')
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeUndefined()
  })

  it('does not silently select inactive or cross-organization services', async () => {
    credentialStore.services = [{ ...env, is_active: false }]
    const wrapper = mountDialog('svc-env')
    await flushPromises()
    expect(wrapper.find('[data-testid="credential-service-context-error"]').text()).toContain(
      'inactive',
    )
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(credentialStore.connectOAuthService).not.toHaveBeenCalled()
  })
})
