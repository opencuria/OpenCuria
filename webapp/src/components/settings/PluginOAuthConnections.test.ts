import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PluginOAuthConnections from './PluginOAuthConnections.vue'

const pluginMock = vi.hoisted(() => ({
  mcpOAuthLoading: {} as Record<string, boolean>,
  status: undefined as { personal: { connected: boolean; reconnect_required: boolean }; organization: { connected: boolean; reconnect_required: boolean } } | undefined,
  fetchMcpOAuthStatus: vi.fn(),
  getMcpOAuthStatus: vi.fn(() => pluginMock.status),
  connectMcpOAuth: vi.fn(),
  disconnectMcpOAuth: vi.fn(),
}))
const authMock = vi.hoisted(() => ({ isAdmin: false }))
vi.mock('@/stores/plugins', () => ({ usePluginStore: () => pluginMock }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => authMock }))

const plugin = {
  id: 'plugin-1',
  name: 'Notion',
  enabled: true,
  published: true,
  credential_requirements: [{ key: 'notion_oauth', service_id: 'service-1', service_name: 'Notion OAuth', service_slug: 'notion-oauth' }],
}
const server = { id: 'server-1', name: 'Notion', oauth_requirement_key: 'notion_oauth', auth_type: 'oauth' }

describe('PluginOAuthConnections', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    pluginMock.status = undefined
    pluginMock.mcpOAuthLoading = {}
    authMock.isAdmin = false
  })

  it('connects a personal account and navigates through plugin store', async () => {
    const wrapper = mount(PluginOAuthConnections, { props: { plugin: plugin as never, server: server as never } })
    await flushPromises()
    expect(pluginMock.fetchMcpOAuthStatus).toHaveBeenCalledWith('plugin-1', 'server-1')
    await wrapper.find('[data-testid="oauth-personal-connect-server-1"]').trigger('click')
    expect(pluginMock.connectMcpOAuth).toHaveBeenCalledWith('plugin-1', 'server-1', 'service-1', false)
    expect(wrapper.find('[data-testid="oauth-organization-connect-server-1"]').exists()).toBe(false)
  })

  it('disables OAuth connect actions while the plugin is unpublished', async () => {
    authMock.isAdmin = true
    const wrapper = mount(PluginOAuthConnections, {
      props: { plugin: { ...plugin, published: false } as never, server: server as never },
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-personal-connect-server-1"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="oauth-organization-connect-server-1"]').attributes('disabled')).toBeDefined()
    await wrapper.find('[data-testid="oauth-personal-connect-server-1"]').trigger('click')
    expect(pluginMock.connectMcpOAuth).not.toHaveBeenCalled()
  })

  it('shows reconnect/disconnect and limits organization actions to admins', async () => {
    authMock.isAdmin = true
    pluginMock.status = {
      personal: { connected: true, reconnect_required: false },
      organization: { connected: false, reconnect_required: true },
    }
    const wrapper = mount(PluginOAuthConnections, { props: { plugin: plugin as never, server: server as never } })
    await flushPromises()
    expect(wrapper.find('[data-testid="oauth-personal-connect-server-1"]').text()).toContain('Reconnect')
    expect(wrapper.find('[data-testid="oauth-personal-disconnect-server-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="oauth-organization-connect-server-1"]').text()).toContain('Reconnect')
    await wrapper.find('[data-testid="oauth-personal-disconnect-server-1"]').trigger('click')
    expect(pluginMock.disconnectMcpOAuth).toHaveBeenCalledWith('plugin-1', 'server-1', 'service-1', false)
  })
})
