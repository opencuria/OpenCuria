import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import PluginsPanel from './PluginsPanel.vue'
import type { Plugin } from '@/types'

const routerPush = vi.fn()
const routerReplace = vi.fn(async () => undefined)
const routeState = reactive({ path: '/', query: {} as Record<string, unknown> })
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush, replace: routerReplace }),
  useRoute: () => routeState,
}))
const pluginStore = vi.hoisted(() => ({
  plugins: [] as Plugin[],
  globalPlugins: [] as Plugin[],
  orgPlugins: [] as Plugin[],
  loading: false,
  error: null as string | null,
  loadedOrgId: 'org-1' as string | null,
  togglingIds: [] as string[],
  reload: vi.fn(),
  toggleActivation: vi.fn(),
  deletePlugin: vi.fn(),
}))
vi.mock('@/stores/plugins', () => ({ usePluginStore: () => pluginStore }))
const auth = vi.hoisted(() => ({ isAdmin: false, activeOrganizationId: 'org-1' }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({ services: [], fetchServices: vi.fn() }),
}))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

function plugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: 'p-1',
    name: 'Notion tools',
    slug: 'notion-tools',
    description: 'Connect Notion to your workspace.',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: true,
    org_enabled: true,
    skills: [
      { id: 's-1', name: 'Search', slug: 'search', body: '# Search\nSafe text', position: 0 },
    ],
    mcp_servers: [
      {
        id: 'm-1',
        name: 'Notion MCP',
        slug: 'notion-mcp',
        transport: 'streamable_http',
        command: '',
        args: [],
        cwd: '/workspace',
        env: {},
        url: 'https://mcp.notion.test/mcp',
        headers: {},
        startup_timeout_seconds: 30,
        request_timeout_seconds: 60,
        auth_type: 'oauth',
        oauth_requirement_key: 'notion',
      },
    ],
    credential_requirements: [
      {
        id: 'r-1',
        key: 'notion',
        description: 'Authorize a Notion account.',
        required: true,
        service_id: 'svc-1',
        service_name: 'Notion OAuth',
        service_slug: 'notion-oauth',
        credential_type: 'mcp_oauth',
      },
    ],
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
    ...overrides,
  }
}
function mountPanel(): ReturnType<typeof mount> {
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
      },
    },
  })
}
describe('PluginsPanel overview and details', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.isAdmin = false
    pluginStore.plugins = []
    pluginStore.globalPlugins = []
    pluginStore.orgPlugins = []
    pluginStore.loading = false
    pluginStore.loadedOrgId = 'org-1'
    pluginStore.error = null
    routeState.path = '/'
    routeState.query = {}
  })
  it('keeps a plugin query deep link selected while the catalog is empty during an org reload', async () => {
    routeState.query = { plugin: 'p-1' }
    pluginStore.plugins = []
    pluginStore.loading = true
    pluginStore.loadedOrgId = null
    const wrapper = mountPanel()
    await flushPromises()
    expect((wrapper.vm as unknown as { selectedId: string | null }).selectedId).toBe('p-1')
    expect(wrapper.find('[data-testid="plugins-error"]').exists()).toBe(false)
  })

  it('shows an unavailable detail state for a requested plugin ID instead of falling back to the list', async () => {
    routeState.query = { plugin: 'missing-plugin' }
    pluginStore.loadedOrgId = 'org-1'
    pluginStore.loading = false
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-detail-unavailable"]').exists()).toBe(true)
    await wrapper.find('[data-testid="plugin-back"]').trigger('click')
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: {} })
  })

  it('shows active availability and correctly pluralized compact catalog counts', async () => {
    const p = plugin()
    pluginStore.plugins = [p]
    pluginStore.globalPlugins = [p]
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-open-p-1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Notion tools')
    expect(wrapper.text()).toContain('Active')
    expect(wrapper.text()).toContain('1 skill')
    expect(wrapper.text()).toContain('1 MCP server')
    expect(wrapper.text()).toContain('1 service')
    expect(wrapper.text()).not.toContain('OAuth connections')
    expect(wrapper.text()).not.toContain('Org credentials')
    expect(wrapper.find('[data-testid="plugin-toggle-p-1"]').exists()).toBe(false)
    await wrapper.find('[data-testid="plugin-open-p-1"]').trigger('click')
    expect(wrapper.find('[data-testid="plugin-detail"]').exists()).toBe(true)
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: { plugin: 'p-1' } })
  })
  it('details expand skill as safe text, show static server and service dependency with deep link', async () => {
    const p = plugin()
    pluginStore.plugins = [p]
    const wrapper = mountPanel()
    await wrapper.find('[data-testid="plugin-open-p-1"]').trigger('click')
    expect(wrapper.text()).toContain('Authorize a Notion account.')
    expect(wrapper.text()).toContain('https://mcp.notion.test/mcp')
    expect(wrapper.text()).toContain('Streamable HTTP')
    expect(wrapper.text()).toContain('OAuth')
    expect(wrapper.text()).not.toContain('mcp_oauth')
    expect(wrapper.find('[data-testid="plugin-add-credential-svc-1"]').exists()).toBe(true)
    await wrapper.find('[data-testid="plugin-add-credential-svc-1"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/',
      query: { settings: 'credentials', add_credential: 'svc-1' },
    })
    await wrapper.find('button[aria-expanded="false"]').trigger('click')
    expect(wrapper.find('pre').text()).toContain('# Search')
    expect(wrapper.find('pre').element.innerHTML).not.toContain('<h1>')
  })
  it('only admins see org activation and org-plugin edit/delete inside details', async () => {
    const orgPlugin = plugin({
      id: 'org-plugin',
      name: 'Local',
      is_global: false,
      organization_id: 'org-1',
    })
    pluginStore.plugins = [orgPlugin]
    pluginStore.orgPlugins = [orgPlugin]
    let wrapper = mountPanel()
    await wrapper.find('[data-testid="plugin-open-org-plugin"]').trigger('click')
    expect(wrapper.find('[data-testid="plugin-toggle-org-plugin"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="plugin-edit-detail"]').exists()).toBe(false)
    wrapper.unmount()
    auth.isAdmin = true
    wrapper = mountPanel()
    await wrapper.find('[data-testid="plugin-open-org-plugin"]').trigger('click')
    expect(wrapper.find('[data-testid="plugin-toggle-org-plugin"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="plugin-edit-detail"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="plugin-delete-detail"]').exists()).toBe(true)
  })
})
