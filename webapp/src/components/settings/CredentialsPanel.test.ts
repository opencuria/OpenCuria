import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CredentialsPanel from './CredentialsPanel.vue'
import type { Credential } from '@/types'
import { saveWorkspaceDraft } from '@/lib/workspaceDraft'

const credentials = vi.hoisted(() => ({
  credentials: [] as Credential[],
  services: [] as never[],
  loading: false,
  error: null as string | null,
  fetchCredentials: vi.fn(),
  fetchServices: vi.fn(),
  reconnectOAuthCredential: vi.fn(),
  disconnectOAuthCredential: vi.fn(),
  updateCredential: vi.fn(),
  deleteCredential: vi.fn(),
  getPublicKey: vi.fn(),
  createCredential: vi.fn(),
  connectOAuthService: vi.fn(),
}))
const auth = vi.hoisted(() => ({
  isAdmin: false,
  user: { id: 1 },
  activeOrganizationId: 'org-1',
  initialized: true,
}))
const notices = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
const routeState = vi.hoisted(() => ({ path: '/', query: {} as Record<string, unknown> }))
const replace = vi.hoisted(() => vi.fn(async () => undefined))
const push = vi.hoisted(() => vi.fn(async () => undefined))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentials }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/notifications', () => ({ useNotificationStore: () => notices }))
vi.mock('@/composables/usePolling', () => ({ usePolling: () => ({ start: vi.fn() }) }))
vi.mock('vue-router', () => ({ useRoute: () => routeState, useRouter: () => ({ replace, push }) }))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const oauth = (values: Partial<Credential> = {}): Credential => ({
  id: 'oauth-1',
  name: 'Notion Work',
  scope: 'personal',
  service_id: 'svc-oauth',
  service_name: 'Notion',
  service_slug: 'notion',
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
  ...values,
})
const stubs = {
  SettingsSection: { template: '<div><slot name="actions"/><slot/></div>' },
  CreateCredentialDialog: { template: '<div />', props: ['open', 'preselectedServiceId'] },
  CredentialCard: { template: '<div />' },
  EmptyState: { template: '<div><slot /></div>' },
  LoadingSpinner: { template: '<span />' },
  EditCredentialDialog: { template: '<div />' },
  DeleteCredentialDialog: { template: '<div />' },
  PublicKeyDialog: { template: '<div />' },
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}
function mountPanel(props: Record<string, unknown> = {}) {
  setActivePinia(createPinia())
  const initialCredentials = [...credentials.credentials]
  credentials.fetchCredentials.mockImplementation(async () => {
    credentials.credentials = initialCredentials
  })
  return mount(CredentialsPanel, { props, global: { stubs } })
}

describe('CredentialsPanel OAuth credential controls', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    credentials.credentials = [oauth()]
    credentials.services = []
    credentials.loading = false
    credentials.error = null
    auth.isAdmin = false
    auth.user = { id: 1 }
    routeState.path = '/'
    routeState.query = {}
    credentials.fetchCredentials.mockResolvedValue(undefined)
    credentials.fetchServices.mockResolvedValue(undefined)
    credentials.reconnectOAuthCredential.mockResolvedValue(true)
    credentials.disconnectOAuthCredential.mockResolvedValue(true)
  })

  it('uses real connected status and owner-specific controls for personal accounts', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-credential-oauth-1"]').text()).toContain('Connected')
    expect(wrapper.find('[data-testid="oauth-reconnect-oauth-1"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="oauth-rename-oauth-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="oauth-delete-oauth-1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Tokens are never shown')
  })

  it('keeps multiple account rows independent and enforces organization ownership for members', async () => {
    credentials.credentials = [
      oauth({ id: 'personal', name: 'Personal Notion' }),
      oauth({
        id: 'organization',
        name: 'Shared Notion',
        scope: 'organization',
        created_by_id: 2,
        oauth_connected: false,
        oauth_reconnect_required: true,
      }),
    ]
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-credential-personal"]').text()).toContain('Connected')
    expect(wrapper.find('[data-testid="oauth-credential-organization"]').text()).toContain(
      'Reconnect required',
    )
    expect(wrapper.find('[data-testid="oauth-rename-personal"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="oauth-rename-organization"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="oauth-delete-organization"]').exists()).toBe(false)
  })

  it('shows reconnect and disconnect for stale grants, with org controls admin-only', async () => {
    credentials.credentials = [
      oauth({
        id: 'stale',
        scope: 'organization',
        created_by_id: 2,
        oauth_connected: false,
        oauth_status: 'reconnect_required',
        oauth_reconnect_required: true,
      }),
    ]
    let wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-reconnect-stale"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="oauth-rename-stale"]').exists()).toBe(false)
    wrapper.unmount()
    auth.isAdmin = true
    wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-reconnect-stale"]').exists()).toBe(true)
    await wrapper.find('[data-testid="oauth-disconnect-stale"]').trigger('click')
    await wrapper.find('[data-testid="oauth-disconnect-confirm"]').trigger('click')
    await flushPromises()
    expect(credentials.disconnectOAuthCredential).toHaveBeenCalledWith('stale')
  })

  it('handles callback once, refreshes real data and removes callback query safely', async () => {
    routeState.query = { oauth_result: 'connected', credential_id: 'oauth-1' }
    const wrapper = mountPanel()
    await flushPromises()
    expect(notices.success).toHaveBeenCalledWith(
      'OAuth connected',
      'Your account connection is ready to use.',
    )
    expect(credentials.fetchCredentials).toHaveBeenCalled()
    expect(replace).toHaveBeenCalledWith({ path: '/', query: {} })
    expect(wrapper.text()).toContain('Connected')
  })

  it('does not auto-return after OAuth; an explicit button resumes the persisted workspace draft', async () => {
    routeState.query = { oauth_result: 'connected', credential_id: 'oauth-1' }
    const draft = saveWorkspaceDraft(
      { mode: 'create', name: 'Keep me', credentialIds: [], pluginIds: [] },
      { userId: 1, organizationId: 'org-1' },
      '/',
    )
    const wrapper = mountPanel({ workspaceDraftId: draft.id })
    await flushPromises()
    expect(push).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="workspace-draft-back"]').exists()).toBe(true)
    await wrapper.find('[data-testid="workspace-draft-back"]').trigger('click')
    await flushPromises()
    expect(push).toHaveBeenCalledWith({ path: '/', query: { resume_workspace: draft.id } })
    expect(replace).toHaveBeenCalledWith({ path: '/', query: {} })
  })

  it('reports failed reconnect safely without replacing provider-managed material', async () => {
    credentials.credentials = [
      oauth({
        oauth_connected: false,
        oauth_status: 'reconnect_required',
        oauth_reconnect_required: true,
      }),
    ]
    credentials.reconnectOAuthCredential.mockResolvedValue(false)
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.find('[data-testid="oauth-reconnect-oauth-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-error"]').text()).toContain(
      'Reconnection could not be started',
    )
  })
})
