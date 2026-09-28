import { beforeEach, describe, expect, it, vi } from 'vitest'
import { disconnectMcpOAuth, getMcpOAuthStatus, startMcpOAuth, validateAuthorizationUrl, navigateToAuthorization } from './mcpOAuth.api'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

describe('MCP OAuth API', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    localStorage.setItem('kern_access_token', 'jwt-test')
    localStorage.setItem('kern_active_org_id', 'org-test')
    fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ personal: { connected: false }, organization: { connected: false } }) })
  })

  it('uses JWT+org headers and includes credentials for flow binding cookie', async () => {
    await getMcpOAuthStatus('plugin/1', 'server?2')
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/mcp-oauth/plugin%2F1/mcp-servers/server%3F2/oauth/status/'),
      expect.objectContaining({ credentials: 'include', headers: expect.objectContaining({ Authorization: 'Bearer jwt-test', 'X-Organization-Id': 'org-test' }) }),
    )
  })

  it('posts scope and service and sends disconnect query', async () => {
    await startMcpOAuth('plugin', 'server', 'service', true)
    expect(fetchMock.mock.calls[0]![1]).toMatchObject({ method: 'POST', body: JSON.stringify({ service_id: 'service', organization_credential: true }) })
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 })
    await disconnectMcpOAuth('plugin', 'server', 'service', false)
    expect(fetchMock.mock.calls[1]![0]).toContain('service_id=service&organization_credential=false')
  })

  it('only accepts safe HTTPS authorization URLs and navigates the same tab', () => {
    expect(validateAuthorizationUrl('https://provider.example/authorize')).toBe('https://provider.example/authorize')
    expect(() => validateAuthorizationUrl('http://provider.example/authorize')).toThrow()
    expect(() => validateAuthorizationUrl('https://user:pass@provider.example/authorize')).toThrow()
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
    navigateToAuthorization('https://provider.example/authorize')
    expect(assign).toHaveBeenCalledWith('https://provider.example/authorize')
    expect(() => navigateToAuthorization('javascript:alert(1)')).toThrow()
  })
})
