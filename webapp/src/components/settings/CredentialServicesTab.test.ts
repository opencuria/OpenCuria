import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import CredentialServicesTab from './CredentialServicesTab.vue'

const auth = vi.hoisted(() => ({ activeOrganizationId: 'org-1' }))
const credentialStore = vi.hoisted(() => ({ services: [] as unknown[], fetchServices: vi.fn() }))
const api = vi.hoisted(() => ({
  listOrganizationCredentialServices: vi.fn(),
  listCredentialServices: vi.fn(),
  toggleOrganizationCredentialService: vi.fn(),
  createOrganizationCredentialService: vi.fn(),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentialStore }))
vi.mock('@/services/credentials.api', () => api)

const globalService = {
  id: 'global-1',
  name: 'Global Notion',
  slug: 'notion',
  description: 'Shared OAuth',
  credential_type: 'mcp_oauth',
  env_var_name: '',
  target_path: '',
  label: '',
  organization_id: null,
  oauth_server_url: 'https://mcp.notion.test/mcp',
  is_active: true,
}
const orgService = {
  id: 'org-1',
  name: 'Local GitHub',
  slug: 'github',
  description: 'Org token',
  credential_type: 'env',
  env_var_name: 'GITHUB_TOKEN',
  target_path: '',
  label: '',
  organization_id: 'org-1',
  oauth_server_url: '',
  is_active: false,
}
const stubs = {
  SettingsSection: { template: '<div><slot name="actions"/><slot/></div>' },
  SettingsRow: { template: '<div><slot name="icon"/><slot/><slot name="actions"/></div>' },
  EmptyState: { template: '<div><slot/></div>' },
  LoadingSpinner: { template: '<span />' },
  Button: { template: '<button><slot /></button>' },
  Label: { template: '<label><slot /></label>' },
  Switch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button @click="$emit(\'update:modelValue\', !modelValue)"><slot /></button>',
  },
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  Select: {
    name: 'Select',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div><slot /></div>',
  },
  SelectContent: { template: '<div><slot /></div>' },
  SelectTrigger: { template: '<button><slot /></button>' },
  SelectValue: { template: '<span><slot /></span>' },
  SelectItem: { template: '<div><slot /></div>' },
}
function mountTab() {
  return mount(CredentialServicesTab, { global: { stubs } })
}

describe('CredentialServicesTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.activeOrganizationId = 'org-1'
    credentialStore.services = []
    api.listOrganizationCredentialServices.mockResolvedValue([globalService, orgService])
    api.listCredentialServices.mockResolvedValue([globalService, orgService])
    api.toggleOrganizationCredentialService.mockResolvedValue({ ...orgService, is_active: true })
    api.createOrganizationCredentialService.mockResolvedValue({
      ...orgService,
      id: 'new',
      is_active: true,
    })
  })

  it('shows global vs org ownership, fixed OAuth endpoint, and activation independently', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.text()).toContain('Global')
    expect(wrapper.text()).toContain('Organization-owned')
    expect(wrapper.text()).toContain('https://mcp.notion.test/mcp')
    expect(wrapper.text()).toContain('Deactivation later only gates new credentials')
    await wrapper.find('[data-testid="service-toggle-org-1"]').trigger('click')
    expect(api.toggleOrganizationCredentialService).toHaveBeenCalledWith('org-1', true)
  })

  it('creates an organization-owned OAuth service using the exact fixed HTTPS endpoint', async () => {
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.find('[data-testid="service-create"]').trigger('click')
    expect(wrapper.text()).toContain('Creates an organization-owned service')
    const selects = wrapper.findAllComponents({ name: 'Select' })
    expect(selects).toHaveLength(1)
    selects[0]!.vm.$emit('update:modelValue', 'mcp_oauth')
    await flushPromises()
    await wrapper.find('#service-name').setValue('Notion OAuth')
    await wrapper.find('#service-oauth-url').setValue('https://mcp.notion.test/mcp')
    await wrapper.find('#create-service-form').trigger('submit')
    await flushPromises()
    expect(api.createOrganizationCredentialService).toHaveBeenCalledWith({
      name: 'Notion OAuth',
      slug: '',
      description: '',
      credential_type: 'mcp_oauth',
      label: '',
      oauth_server_url: 'https://mcp.notion.test/mcp',
    })
  })
})
