import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CredentialsPanel from './CredentialsPanel.vue'
import CredentialCard from '@/components/credentials/CredentialCard.vue'
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
  EmptyState: { template: '<div><slot /></div>' },
  LoadingSpinner: { template: '<span />' },
  EditCredentialDialog: {
    name: 'EditCredentialDialog',
    template: '<div />',
    props: ['credential'],
  },
  DeleteCredentialDialog: {
    name: 'DeleteCredentialDialog',
    template: '<div />',
    props: ['credential'],
  },
  PublicKeyDialog: { template: '<div />' },
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
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
    expect(wrapper.text()).not.toContain('Tokens are never shown')
    expect(wrapper.text()).toContain('Created')
  })

  it('renders OAuth and ordinary credentials through the same card and icon actions', async () => {
    credentials.credentials = [
      oauth(),
      oauth({
        id: 'env-1',
        name: 'GitHub Work',
        service_name: 'GitHub',
        credential_type: 'env',
        env_var_name: 'GITHUB_TOKEN',
      }),
    ]
    const wrapper = mountPanel()
    await flushPromises()
    const cards = wrapper.findAllComponents(CredentialCard)
    expect(cards).toHaveLength(2)
    for (const card of cards) {
      expect(card.findAll('[data-slot="badge"]')).toHaveLength(2)
      expect(card.text()).toContain('Personal')
      expect(card.text()).toContain('Created')
      for (const action of ['Edit credential', 'Delete credential']) {
        const button = card.find(`button[aria-label="${action}"]`)
        expect(button.attributes('data-variant')).toBe('ghost')
        expect(button.attributes('data-size')).toBe('icon-sm')
        expect(button.text()).toBe('')
        expect(button.find('svg').exists()).toBe(true)
      }
    }
    expect(cards[0]!.text()).toContain('OAuth')
    expect(cards[1]!.text()).toContain('ENV')
    expect(wrapper.find('[data-testid="credential-env-1"]').text()).toContain('GITHUB_TOKEN')
  })

  it('does not expose personal account controls to another user, including admins', async () => {
    credentials.credentials = [oauth({ created_by_id: 2 })]
    auth.isAdmin = true
    const wrapper = mountPanel({ initialCredentialId: 'oauth-1' })
    await flushPromises()
    const row = wrapper.find('[data-testid="oauth-credential-oauth-1"]')
    expect(row.find('button').exists()).toBe(false)
    expect(credentials.reconnectOAuthCredential).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="oauth-error"]').text()).toContain('permission')
  })

  it('forwards edit and delete actions to the existing credential dialogs', async () => {
    const credential = credentials.credentials[0]
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.find('[data-testid="oauth-rename-oauth-1"]').trigger('click')
    expect(wrapper.findComponent({ name: 'EditCredentialDialog' }).props('credential')).toEqual(
      credential,
    )
    await wrapper.find('[data-testid="oauth-delete-oauth-1"]').trigger('click')
    expect(wrapper.findComponent({ name: 'DeleteCredentialDialog' }).props('credential')).toEqual(
      credential,
    )
  })

  it('keeps the disconnect confirmation open when the provider grant cannot be cleared', async () => {
    credentials.disconnectOAuthCredential.mockResolvedValue(false)
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.find('[data-testid="oauth-disconnect-oauth-1"]').trigger('click')
    expect(wrapper.text()).toContain('Disconnect Credential')
    expect(wrapper.text()).toContain('Workspace associations are preserved.')
    await wrapper.find('[data-testid="oauth-disconnect-confirm"]').trigger('click')
    await flushPromises()
    expect(credentials.disconnectOAuthCredential).toHaveBeenCalledWith('oauth-1')
    expect(wrapper.find('[data-testid="oauth-error"]').text()).toContain(
      'could not be disconnected',
    )
    expect(
      (wrapper.vm as unknown as { disconnectingCredential: Credential }).disconnectingCredential.id,
    ).toBe('oauth-1')
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

  it('offers only reconnect, edit and delete for a disconnected OAuth credential', async () => {
    credentials.credentials = [oauth({ oauth_connected: false, oauth_status: 'disconnected' })]
    const wrapper = mountPanel()
    await flushPromises()
    const row = wrapper.find('[data-testid="oauth-credential-oauth-1"]')
    expect(row.text()).toContain('Not connected')
    expect(row.find('[data-testid="oauth-reconnect-oauth-1"]').exists()).toBe(true)
    expect(row.find('[data-testid="oauth-disconnect-oauth-1"]').exists()).toBe(false)
    expect(row.findAll('button')).toHaveLength(3)
  })

  it('disables the shared reconnect icon while starting provider authorization', async () => {
    credentials.credentials = [oauth({ oauth_connected: false })]
    let finish!: (success: boolean) => void
    credentials.reconnectOAuthCredential.mockImplementationOnce(
      () =>
        new Promise<boolean>((resolve) => {
          finish = resolve
        }),
    )
    const wrapper = mountPanel()
    await flushPromises()
    const button = wrapper.find('[data-testid="oauth-reconnect-oauth-1"]')
    await button.trigger('click')
    await flushPromises()
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.find('svg').classes()).toContain('animate-spin')
    expect(credentials.reconnectOAuthCredential).toHaveBeenCalledWith('oauth-1', 'Notion Work')
    finish(true)
    await flushPromises()
    expect(button.attributes('disabled')).toBeUndefined()
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
