import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PluginsPanel from './PluginsPanel.vue'

const push = vi.fn()
const replace = vi.fn(async () => {})
const route = {
  path: '/',
  query: { settings: 'plugins', mcp_oauth: 'connected' as string | undefined },
}
const notifications = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}))
const pluginStore = vi.hoisted(() => ({
  plugins: [] as never[],
  globalPlugins: [] as never[],
  orgPlugins: [] as never[],
  loading: false,
  error: null as string | null,
  togglingIds: [] as string[],
  reload: vi.fn(async () => {}),
  mcpOAuthStatuses: {},
  mcpOAuthLoading: {},
  getMcpOAuthStatus: vi.fn(),
  fetchMcpOAuthStatus: vi.fn(),
}))
const credentialStore = vi.hoisted(() => ({ fetchCredentials: vi.fn(async () => {}) }))
const auth = vi.hoisted(() => ({ isAdmin: false, activeOrganizationId: 'org-1' }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push, replace }), useRoute: () => route }))
vi.mock('@/stores/plugins', () => ({ usePluginStore: () => pluginStore }))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentialStore }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/notifications', () => ({ useNotificationStore: () => notifications }))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const stubs = {
  PluginEditorDialog: { template: '<div />' },
  PluginOAuthConnections: { template: '<div />' },
  Dialog: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

describe('PluginsPanel OAuth return notice', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    route.query.settings = 'plugins'
    route.query.mcp_oauth = 'connected'
    pluginStore.plugins = []
    pluginStore.globalPlugins = []
    pluginStore.orgPlugins = []
  })

  it('shows a success notice, reloads state, and keeps the Plugins deep link', async () => {
    setActivePinia(createPinia())
    mount(PluginsPanel, { global: { stubs } })
    await flushPromises()
    expect(notifications.success).toHaveBeenCalledWith('OAuth connected', expect.any(String))
    expect(pluginStore.reload).toHaveBeenCalled()
    expect(credentialStore.fetchCredentials).toHaveBeenCalled()
    expect(replace).toHaveBeenCalledWith({ path: '/', query: { settings: 'plugins' } })
  })

  it('shows an error notice for a failed callback without exposing callback data', async () => {
    route.query.settings = 'plugins'
    route.query.mcp_oauth = 'error'
    setActivePinia(createPinia())
    mount(PluginsPanel, { global: { stubs } })
    await flushPromises()
    expect(notifications.error).toHaveBeenCalledWith('OAuth connection failed', expect.any(String))
    expect(replace).toHaveBeenCalledWith({ path: '/', query: { settings: 'plugins' } })
  })
})
