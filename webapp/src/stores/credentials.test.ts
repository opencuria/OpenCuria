import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import * as api from '@/services/credentials.api'
import { useCredentialStore, validateAuthorizationUrl } from './credentials'
import { toast } from 'vue-sonner'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))
vi.mock('@/services/credentials.api', () => ({
  listCredentialServices: vi.fn(),
  listCredentials: vi.fn(),
  createCredential: vi.fn(),
  connectOAuthService: vi.fn(),
  reconnectOAuthCredential: vi.fn(),
  disconnectOAuthCredential: vi.fn(),
  getPublicKey: vi.fn(),
  updateCredential: vi.fn(),
  deleteCredential: vi.fn(),
}))

describe('credential store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads real credential service/account state', async () => {
    vi.mocked(api.listCredentials).mockResolvedValue([])
    vi.mocked(api.listCredentialServices).mockResolvedValue([])
    const store = useCredentialStore()
    await Promise.all([store.fetchCredentials(), store.fetchServices()])
    expect(store.credentials).toEqual([])
    expect(store.services).toEqual([])
    expect(api.listCredentials).toHaveBeenCalledOnce()
  })

  it('navigates same-tab only to credential-free HTTPS provider URLs', async () => {
    vi.mocked(api.connectOAuthService).mockResolvedValue({
      authorization_url: 'https://provider.test/authorize',
      status: 'pending',
    })
    const store = useCredentialStore()
    store.services = [
      {
        id: 'svc-1',
        name: 'Notion',
        slug: 'notion',
        description: '',
        credential_type: 'mcp_oauth',
        env_var_name: '',
        target_path: '',
        label: '',
        oauth_server_url: 'https://mcp.notion.test/mcp',
        organization_id: 'org-1',
        is_active: true,
      },
    ]
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
    await expect(store.connectOAuthService('svc-1', 'Notion', false)).resolves.toBe(true)
    expect(api.connectOAuthService).toHaveBeenCalledWith('svc-1', {
      name: 'Notion',
      organization_credential: false,
    })
    expect(assign).toHaveBeenCalledWith('https://provider.test/authorize')
    expect(() => validateAuthorizationUrl('http://provider.test/authorize')).toThrow()
    expect(() => validateAuthorizationUrl('https://user:pass@provider.test/authorize')).toThrow()
  })

  it('routes reconnect/disconnect through credential endpoints and surfaces safe errors', async () => {
    vi.mocked(api.reconnectOAuthCredential).mockResolvedValue({
      authorization_url: 'https://provider.test/authorize',
      status: 'pending',
    })
    vi.mocked(api.disconnectOAuthCredential).mockRejectedValue(
      new Error('Required workspace dependency'),
    )
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
    const store = useCredentialStore()
    await expect(store.reconnectOAuthCredential('cred-1', 'Account')).resolves.toBe(true)
    expect(api.reconnectOAuthCredential).toHaveBeenCalledWith('cred-1', 'Account')
    await expect(store.disconnectOAuthCredential('cred-1')).resolves.toBe(false)
    expect(api.disconnectOAuthCredential).toHaveBeenCalledWith('cred-1')
    expect(toast.error).toHaveBeenCalledWith(
      'Disconnect failed',
      expect.objectContaining({ description: 'Required workspace dependency' }),
    )
  })
})
