import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as http from './api'
import {
  createCredential,
  createOrganizationCredentialService,
  deleteCredential,
  listCredentialServices,
  listCredentials,
  toggleOrganizationCredentialService,
  updateCredential,
} from './credentials.api'

vi.mock('./api', () => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() }))

describe('credential REST APIs', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists the real credential/service records', async () => {
    vi.mocked(http.get).mockResolvedValueOnce([]).mockResolvedValueOnce([])
    await listCredentialServices()
    await listCredentials()
    expect(http.get).toHaveBeenNthCalledWith(1, '/credential-services/')
    expect(http.get).toHaveBeenNthCalledWith(2, '/credentials/')
  })

  it('creates and renames regular credentials and uses organization service APIs', async () => {
    await createCredential({ service_id: 'svc-id', name: 'GitHub', value: 'encrypted-input' })
    expect(http.post).toHaveBeenCalledWith('/credentials/', {
      service_id: 'svc-id',
      name: 'GitHub',
      value: 'encrypted-input',
    })
    await updateCredential('cred-id', { name: 'Renamed' })
    expect(http.patch).toHaveBeenCalledWith('/credentials/cred-id/', { name: 'Renamed' })
  })

  it('uses organization service APIs and never wraps credentials in plugin APIs', async () => {
    await createOrganizationCredentialService({
      name: 'Notion',
      credential_type: 'mcp_oauth',
      oauth_server_url: 'https://mcp.notion.test/mcp',
    })
    expect(http.post).toHaveBeenCalledWith(
      '/org-credential-services/',
      expect.objectContaining({
        credential_type: 'mcp_oauth',
        oauth_server_url: 'https://mcp.notion.test/mcp',
      }),
    )
    await toggleOrganizationCredentialService('svc-id', false)
    expect(http.post).toHaveBeenCalledWith('/org-credential-services/svc-id/activation/', {
      active: false,
    })
    await deleteCredential('credential-id')
    expect(http.del).toHaveBeenCalledWith('/credentials/credential-id/')
  })
})
