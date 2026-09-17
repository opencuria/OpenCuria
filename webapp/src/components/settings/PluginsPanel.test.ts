import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import PluginsPanel from './PluginsPanel.vue'
import type { Plugin } from '@/types'

const routerPush = vi.fn()

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const pluginStoreMock = vi.hoisted(() => ({
  plugins: [] as Plugin[],
  globalPlugins: [] as Plugin[],
  orgPlugins: [] as Plugin[],
  loading: false,
  error: null as string | null,
  togglingIds: [] as string[],
  togglingId: null as string | null,
  reload: vi.fn(),
  toggleActivation: vi.fn(),
  deletePlugin: vi.fn(),
}))

vi.mock('@/stores/plugins', () => ({
  usePluginStore: () => pluginStoreMock,
}))

vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({
    credentials: [],
    services: [],
    loading: false,
    error: null,
    fetchCredentials: vi.fn(),
    fetchServices: vi.fn(),
  }),
}))

const authMock = vi.hoisted(() => ({
  isAdmin: false,
  activeOrganizationId: 'org-1',
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authMock,
}))

vi.mock('@/services/plugins.api', () => ({
  listPlugins: vi.fn(async () => []),
  createPlugin: vi.fn(),
  updatePlugin: vi.fn(),
  deletePlugin: vi.fn(),
  togglePluginActivation: vi.fn(),
  listWorkspacePlugins: vi.fn(async () => []),
  updateWorkspacePlugins: vi.fn(),
}))

function makePlugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: 'plugin-1',
    name: 'Playwright',
    slug: 'playwright',
    description: 'Browser automation',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: true,
    org_enabled: false,
    skills: [{ id: 's-1', name: 'Basics', slug: 'basics', body: 'x', position: 0 }],
    mcp_servers: [
      {
        id: 'm-1',
        name: 'Runner',
        slug: 'runner',
        transport: 'stdio',
        command: 'npx',
        args: ['-y'],
        cwd: '/workspace',
        env: {},
        url: '',
        headers: {},
        startup_timeout_seconds: 30,
        request_timeout_seconds: 60,
      },
    ],
    credential_requirements: [],
    credential_readiness: {
      required_service_ids: [],
      missing_required_service_ids: [],
      ready: true,
    },
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function mountPanel() {
  setActivePinia(createPinia())
  return mount(PluginsPanel, {
    global: {
      stubs: {
        PluginEditorDialog: { template: '<div />' },
        Dialog: { template: '<div><slot /></div>' },
        DialogContent: { template: '<div><slot /></div>' },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        DialogFooter: { template: '<div><slot /></div>' },
        ScrollArea: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('PluginsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authMock.isAdmin = false
    pluginStoreMock.loading = false
    pluginStoreMock.error = null
    pluginStoreMock.togglingIds = []
    pluginStoreMock.togglingId = null
    pluginStoreMock.plugins = []
    pluginStoreMock.globalPlugins = []
    pluginStoreMock.orgPlugins = []
  })

  it('member view shows catalog without mutations', async () => {
    const plugin = makePlugin()
    pluginStoreMock.plugins = [plugin]
    pluginStoreMock.globalPlugins = [plugin]
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Playwright')
    expect(wrapper.text()).toContain('OpenCuria')
    expect(wrapper.find('[data-testid="plugin-create"]').exists()).toBe(false)
    expect(wrapper.find(`[data-testid="plugin-toggle-${plugin.id}"]`).exists()).toBe(false)
  })

  it('admin sees toggles/create and can toggle activation', async () => {
    authMock.isAdmin = true
    const plugin = makePlugin()
    pluginStoreMock.plugins = [plugin]
    pluginStoreMock.globalPlugins = [plugin]
    pluginStoreMock.toggleActivation.mockResolvedValue(true)
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-create"]').exists()).toBe(true)
    const toggle = wrapper.find(`[data-testid="plugin-toggle-${plugin.id}"]`)
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('click')
    expect(pluginStoreMock.toggleActivation).toHaveBeenCalledWith(plugin.id, true)
  })

  it('shows setup-needed readiness with a manage-credentials action', async () => {
    const plugin = makePlugin({
      credential_readiness: {
        required_service_ids: ['svc-1'],
        missing_required_service_ids: ['svc-1'],
        ready: false,
      },
    })
    pluginStoreMock.plugins = [plugin]
    pluginStoreMock.globalPlugins = [plugin]
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find(`[data-testid="plugin-readiness-${plugin.id}"]`).text()).toContain(
      'Org credentials missing',
    )
    await wrapper.find(`[data-testid="plugin-manage-credentials-${plugin.id}"]`).trigger('click')
    expect(routerPush).toHaveBeenCalledWith({ path: '/', query: { settings: 'credentials' } })
  })

  it('disables the activation switch for unpublished plugins', async () => {
    authMock.isAdmin = true
    const plugin = makePlugin({ enabled: false })
    pluginStoreMock.plugins = [plugin]
    pluginStoreMock.globalPlugins = [plugin]
    const wrapper = mountPanel()
    await flushPromises()
    const toggle = wrapper.find(`[data-testid="plugin-toggle-${plugin.id}"]`)
    expect(toggle.attributes('disabled')).toBeDefined()
  })

  it('keeps the switch enabled for org-active plugins even when unpublished (disable always allowed)', async () => {
    authMock.isAdmin = true
    const plugin = makePlugin({ enabled: false, org_enabled: true })
    pluginStoreMock.plugins = [plugin]
    pluginStoreMock.globalPlugins = [plugin]
    const wrapper = mountPanel()
    await flushPromises()
    const toggle = wrapper.find(`[data-testid="plugin-toggle-${plugin.id}"]`)
    expect(toggle.attributes('disabled')).toBeUndefined()
  })

  it('labels org credential readiness explicitly', async () => {
    const plugin = makePlugin({
      credential_readiness: {
        required_service_ids: ['svc-1'],
        missing_required_service_ids: ['svc-1'],
        ready: false,
      },
    })
    pluginStoreMock.plugins = [plugin]
    pluginStoreMock.globalPlugins = [plugin]
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find(`[data-testid="plugin-readiness-${plugin.id}"]`).text()).toContain(
      'Org credentials missing',
    )
    expect(wrapper.text()).toContain('Manage organization credentials')
  })
})
