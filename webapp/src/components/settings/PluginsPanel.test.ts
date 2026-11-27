import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import PluginsPanel from './PluginsPanel.vue'
import DetailHeader from '@/components/common/DetailHeader.vue'
import type {
  Plugin,
  PluginCredentialRequirement,
  PluginMcpServer,
} from '@opencuria/chat-core/plugins'

const routerPush = vi.fn()
const routerReplace = vi.fn(async () => undefined)
const routeState = reactive({ path: '/', query: {} as Record<string, unknown> })
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush, replace: routerReplace }),
  useRoute: () => routeState,
}))
const pluginStore = reactive({
  plugins: [] as Plugin[],
  loading: false,
  error: null as string | null,
  loadedOrgId: 'org-1' as string | null,
  togglingIds: [] as string[],
  reload: vi.fn(),
  toggleActivation: vi.fn(async () => true),
  deletePlugin: vi.fn(async () => true),
})
vi.mock('@/stores/plugins', () => ({ usePluginStore: () => pluginStore }))
const auth = reactive({ isAdmin: false, activeOrganizationId: 'org-1' })
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({ services: [], fetchServices: vi.fn() }),
}))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

function server(overrides: Partial<PluginMcpServer> = {}): PluginMcpServer {
  return {
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
    auth_type: 'oauth',
    oauth_requirement_key: 'notion',
    ...overrides,
  }
}

function requirement(
  overrides: Partial<PluginCredentialRequirement> = {},
): PluginCredentialRequirement {
  return {
    id: 'r-1',
    key: 'notion',
    description: 'Authorize a Notion account.',
    required: true,
    service_id: 'svc-1',
    service_name: 'Notion OAuth',
    service_slug: 'notion-oauth',
    credential_type: 'mcp_oauth',
    credential_id: null,
    credential_name: '',
    credential_scope: '',
    ...overrides,
  }
}

function plugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: 'p-1',
    name: 'Notion tools',
    slug: 'notion-tools',
    description: 'Connect Notion to your workspace.',
    scope: 'builtin',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: true,
    org_enabled: true,
    available: true,
    editable: false,
    skills: [
      {
        id: 's-1',
        name: 'Search',
        slug: 'search',
        description: 'Find pages in Notion.',
        body: '# Search\nSafe text',
        position: 0,
      },
    ],
    mcp_servers: [server()],
    credential_requirements: [requirement()],
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
    ...overrides,
  }
}

function personal(overrides: Partial<Plugin> = {}): Plugin {
  return plugin({
    id: 'mine',
    name: 'My tools',
    scope: 'personal',
    is_global: false,
    org_enabled: false,
    editable: true,
    credential_requirements: [],
    ...overrides,
  })
}

function shared(overrides: Partial<Plugin> = {}): Plugin {
  return plugin({
    id: 'team',
    name: 'Team tools',
    scope: 'organization',
    organization_id: 'org-1',
    is_global: false,
    ...overrides,
  })
}

const EditorStub = {
  name: 'PluginEditorDialog',
  props: ['open', 'plugin'],
  emits: ['saved', 'update:open'],
  template: '<div data-testid="editor-stub" />',
}

function mountPanel(): VueWrapper {
  setActivePinia(createPinia())
  return mount(PluginsPanel, {
    global: {
      stubs: {
        PluginEditorDialog: EditorStub,
        Dialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' },
        DialogContent: { template: '<div><slot /></div>' },
        DialogHeader: { template: '<div><slot /></div>' },
        DialogTitle: { template: '<div><slot /></div>' },
        DialogDescription: { template: '<div><slot /></div>' },
        DialogFooter: { template: '<div><slot /></div>' },
      },
    },
  })
}

async function openDetail(wrapper: VueWrapper, id: string): Promise<void> {
  await wrapper.get(`[data-testid="plugin-open-${id}"]`).trigger('click')
  await flushPromises()
}

function catalogSections(wrapper: VueWrapper) {
  return wrapper.get('[data-testid="plugin-catalog"]').findAll('section')
}

describe('PluginsPanel catalog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.isAdmin = false
    pluginStore.plugins = []
    pluginStore.loading = false
    pluginStore.loadedOrgId = 'org-1'
    pluginStore.error = null
    pluginStore.togglingIds = []
    routeState.path = '/'
    routeState.query = {}
  })

  it('keeps a plugin query deep link selected while the catalog is empty during an org reload', async () => {
    routeState.query = { plugin: 'p-1' }
    pluginStore.loading = true
    pluginStore.loadedOrgId = null
    const wrapper = mountPanel()
    await flushPromises()
    expect((wrapper.vm as unknown as { selectedId: string | null }).selectedId).toBe('p-1')
    expect(wrapper.find('[data-testid="plugins-error"]').exists()).toBe(false)
  })

  it('shows an unavailable detail state for a requested plugin ID instead of falling back to the list', async () => {
    routeState.query = { plugin: 'missing-plugin' }
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-detail-unavailable"]').exists()).toBe(true)
    await wrapper.find('[data-testid="plugin-back"]').trigger('click')
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: {} })
  })

  it('shows load failures instead of the catalog', async () => {
    pluginStore.error = 'Network down'
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.get('[data-testid="plugins-error"]').text()).toContain('Network down')
    expect(wrapper.find('[data-testid="plugin-catalog"]').exists()).toBe(false)
  })

  it('groups plugins by owner, own plugins first and by name, and hides empty shared sections', async () => {
    pluginStore.plugins = [
      plugin({ id: 'builtin', name: 'OpenCuria' }),
      personal({ id: 'zeta', name: 'Zeta' }),
      personal({ id: 'alpha', name: 'Alpha', skills: [], mcp_servers: [] }),
    ]
    const wrapper = mountPanel()
    await flushPromises()
    const sections = catalogSections(wrapper)
    expect(sections.map((section) => section.get('h4').text())).toEqual([
      'Your plugins',
      'Built-in',
    ])
    expect(
      sections[0]!
        .findAll('[data-testid^="plugin-open-"]')
        .map((row) => row.attributes('data-testid')),
    ).toEqual(['plugin-open-alpha', 'plugin-open-zeta'])
    expect(wrapper.get('[data-testid="plugin-open-alpha"]').text()).toContain('Empty')
    expect(wrapper.get('[data-testid="plugin-open-zeta"]').text()).toContain(
      '1 skill · 1 MCP server',
    )

    pluginStore.plugins = [...pluginStore.plugins, shared()]
    await flushPromises()
    expect(catalogSections(wrapper).map((section) => section.get('h4').text())).toEqual([
      'Your plugins',
      'Organization',
      'Built-in',
    ])
  })

  it('lets members create plugins and invites them to when they have none', async () => {
    pluginStore.plugins = [plugin()]
    const wrapper = mountPanel()
    await flushPromises()
    const own = catalogSections(wrapper)[0]!
    expect(own.text()).toContain('No plugins of your own yet')
    expect(own.find('[data-testid="plugin-create-empty"]').exists()).toBe(true)

    const editor = wrapper.getComponent(EditorStub)
    expect(editor.props('open')).toBe(false)
    await wrapper.get('[data-testid="plugin-create"]').trigger('click')
    expect(editor.props()).toMatchObject({ open: true, plugin: null })
  })

  it('opens a newly created plugin after saving', async () => {
    const created = personal()
    pluginStore.plugins = [created]
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="plugin-create"]').trigger('click')
    wrapper.getComponent(EditorStub).vm.$emit('saved', created)
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-detail"]').exists()).toBe(true)
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: { plugin: 'mine' } })
  })

  it('marks unreleased shared plugins for admins only', async () => {
    pluginStore.plugins = [
      shared({ org_enabled: false, available: false }),
      plugin({ id: 'draft', name: 'Draft', published: false, org_enabled: false }),
      personal(),
    ]
    auth.isAdmin = true
    const admin = mountPanel()
    await flushPromises()
    expect(admin.get('[data-testid="plugin-open-team"]').text()).toContain('Not released')
    expect(admin.get('[data-testid="plugin-open-draft"]').text()).toContain('Unpublished')
    expect(admin.get('[data-testid="plugin-open-mine"]').text()).not.toMatch(
      /Not released|Unpublished/,
    )
    admin.unmount()

    auth.isAdmin = false
    const member = mountPanel()
    await flushPromises()
    expect(member.text()).not.toMatch(/Not released|Unpublished/)
  })
})

describe('PluginsPanel details', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.isAdmin = false
    pluginStore.plugins = []
    pluginStore.loading = false
    pluginStore.loadedOrgId = 'org-1'
    pluginStore.error = null
    pluginStore.togglingIds = []
    routeState.path = '/'
    routeState.query = {}
  })

  it('uses the shared detail header and reports detail state while opening and leaving', async () => {
    pluginStore.plugins = [plugin()]
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.emitted('detail-change')).toEqual([[false]])
    await openDetail(wrapper, 'p-1')
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: { plugin: 'p-1' } })
    const header = wrapper.findComponent(DetailHeader)
    expect(header.props()).toMatchObject({
      title: 'Notion tools',
      backLabel: 'Plugins',
      description: 'Connect Notion to your workspace.',
    })
    expect(header.text()).toContain('Built-in')
    expect(header.text()).toContain('1 skill · 1 MCP server')
    expect(wrapper.emitted('detail-change')).toEqual([[false], [true]])

    routerReplace.mockClear()
    await wrapper.get('[data-testid="detail-back"]').trigger('click')
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: {} })
    expect(wrapper.find('[data-testid="plugin-catalog"]').exists()).toBe(true)
    expect(wrapper.emitted('detail-change')).toEqual([[false], [true], [false]])
  })

  it('expands skills as safe text and links required credentials to Settings', async () => {
    pluginStore.plugins = [plugin()]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'p-1')
    expect(wrapper.text()).toContain('Find pages in Notion.')
    expect(wrapper.text()).toContain('https://mcp.notion.test/mcp')
    expect(wrapper.text()).toContain('Streamable HTTP')
    expect(wrapper.text()).toContain('OAuth')
    expect(wrapper.text()).not.toContain('mcp_oauth')

    // Without a credential of its own, each workspace attaches one.
    const credentials = wrapper.get('[aria-label="Credentials"]')
    expect(credentials.text()).toContain('Notion OAuth')
    expect(credentials.text()).not.toContain('optional')
    // Where the credential comes from, then what it is for.
    expect(wrapper.get('[data-testid="plugin-credential-source-notion"]').text()).toBe(
      'Each workspace adds its own · Authorize a Notion account.',
    )
    await wrapper.get('[data-testid="plugin-add-credential-svc-1"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/',
      query: { settings: 'credentials', add_credential: 'svc-1' },
    })

    expect(wrapper.find('pre').exists()).toBe(false)
    await wrapper.get('button[aria-expanded="false"]').trigger('click')
    expect(wrapper.get('pre').text()).toContain('# Search')
    expect(wrapper.get('pre').element.innerHTML).not.toContain('<h1>')
  })

  it('shows command servers as a copyable command line and never shows values', async () => {
    pluginStore.plugins = [
      personal({
        mcp_servers: [
          server({
            name: 'Acme',
            transport: 'stdio',
            command: 'npx',
            args: ['-y', '@acme/mcp server'],
            env: { LOG_LEVEL: 'debug', API_TOKEN: '{{credential.api_token}}' },
            url: '',
            auth_type: 'none',
            oauth_requirement_key: '',
          }),
        ],
        credential_requirements: [
          requirement({
            key: 'api_token',
            description: '',
            service_id: 'svc-api',
            service_name: 'API_TOKEN (My tools)',
            service_slug: 'api-token-my-tools',
            credential_type: 'env',
            credential_id: 'cred-api',
            credential_name: 'My tools: API_TOKEN',
            credential_scope: 'personal',
          }),
        ],
      }),
    ]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'mine')
    const servers = wrapper.get('[aria-label="MCP servers"]')
    expect(servers.text()).toContain("npx -y '@acme/mcp server'")
    expect(servers.text()).toContain('Command')
    expect(servers.text()).toContain('LOG_LEVEL')
    expect(servers.text()).toContain('API_TOKEN')
    expect(servers.text()).not.toContain('debug')
    expect(servers.text()).not.toContain('{{credential')
    // Only the secret variable is marked as coming from a credential.
    const secret = servers.get('[title="API_TOKEN comes from a credential"]')
    expect(secret.text()).toBe('API_TOKEN')
    expect(secret.find('svg.lucide-lock').exists()).toBe(true)
    const plain = servers.findAll('.font-mono').find((badge) => badge.text() === 'LOG_LEVEL')!
    expect(plain.attributes('title')).toBeUndefined()
    expect(plain.find('svg').exists()).toBe(false)
  })

  it('shows the headers of URL servers by name and marks secret ones', async () => {
    pluginStore.plugins = [
      personal({
        mcp_servers: [
          server({
            auth_type: 'none',
            oauth_requirement_key: '',
            headers: { Authorization: '{{credential.authorization}}', 'X-Team': 'core' },
            // Environment variables of URL servers are not used.
            env: { IGNORED: 'x' },
          }),
        ],
      }),
    ]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'mine')
    const servers = wrapper.get('[aria-label="MCP servers"]')
    expect(servers.text()).toContain('Authorization')
    expect(servers.text()).toContain('X-Team')
    expect(servers.text()).not.toContain('core')
    expect(servers.text()).not.toContain('IGNORED')
    expect(servers.find('[title="Authorization comes from a credential"]').exists()).toBe(true)
    expect(servers.find('[title="X-Team comes from a credential"]').exists()).toBe(false)
  })

  it('shows the credential a plugin uses and offers Add credential only for the others', async () => {
    pluginStore.plugins = [
      personal({
        credential_requirements: [
          requirement({
            id: 'r-linked',
            key: 'api_token',
            description: '',
            service_id: 'svc-api',
            service_name: 'Acme API',
            service_slug: 'acme-api',
            credential_type: 'env',
            credential_id: 'cred-api',
            credential_name: 'Work token',
            credential_scope: 'personal',
          }),
          requirement({
            id: 'r-ssh',
            key: 'ssh',
            description: '',
            required: false,
            service_id: 'svc-ssh',
            service_name: 'Deploy key',
            service_slug: 'deploy-key',
            credential_type: 'ssh_key',
          }),
        ],
      }),
    ]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'mine')
    expect(wrapper.get('[data-testid="plugin-credential-source-api_token"]').text()).toBe(
      'Uses “Work token”',
    )
    expect(wrapper.find('[data-testid="plugin-add-credential-svc-api"]').exists()).toBe(false)

    expect(wrapper.get('[data-testid="plugin-credential-source-ssh"]').text()).toBe(
      'Each workspace adds its own',
    )
    const credentials = wrapper.get('[aria-label="Credentials"]')
    expect(credentials.text()).toContain('Deploy key · optional')
    expect(credentials.findAll('button').map((button) => button.text())).toEqual([
      'Add credential',
    ])
    await wrapper.get('[data-testid="plugin-add-credential-svc-ssh"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith({
      path: '/',
      query: { settings: 'credentials', add_credential: 'svc-ssh' },
    })
  })

  it('shows the built-in OpenCuria server without an endpoint', async () => {
    pluginStore.plugins = [
      plugin({
        id: 'opencuria',
        name: 'OpenCuria',
        skills: [],
        credential_requirements: [],
        mcp_servers: [server({ name: 'OpenCuria MCP', transport: 'builtin', url: '' })],
      }),
    ]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'opencuria')
    const servers = wrapper.get('[aria-label="MCP servers"]')
    expect(servers.text()).toContain('Built-in')
    expect(servers.find('code').exists()).toBe(false)
  })

  it('calls an empty plugin empty and only suggests editing when the user may', async () => {
    pluginStore.plugins = [
      personal({ skills: [], mcp_servers: [] }),
      plugin({ id: 'empty-builtin', skills: [], mcp_servers: [], credential_requirements: [] }),
    ]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'mine')
    expect(wrapper.get('[data-testid="plugin-detail"]').text()).toContain(
      'This plugin is empty. Edit it to add skills or MCP servers.',
    )
    await wrapper.get('[data-testid="detail-back"]').trigger('click')
    await openDetail(wrapper, 'empty-builtin')
    const detail = wrapper.get('[data-testid="plugin-detail"]').text()
    expect(detail).toContain('This plugin is empty.')
    expect(detail).not.toContain('Edit it')
  })

  it('shows the release switch only to admins and only for shared plugins', async () => {
    pluginStore.plugins = [shared({ org_enabled: false, available: false }), personal()]
    const member = mountPanel()
    await openDetail(member, 'team')
    expect(member.find('[data-testid="plugin-toggle-team"]').exists()).toBe(false)
    expect(member.text()).not.toContain('Not released')
    member.unmount()

    auth.isAdmin = true
    const admin = mountPanel()
    await openDetail(admin, 'team')
    const header = admin.getComponent(DetailHeader)
    expect(header.text()).toContain('Not released')
    const toggle = admin.get('[data-testid="plugin-toggle-team"]')
    expect(toggle.attributes('aria-label')).toBe('Release Team tools')
    await toggle.trigger('click')
    await flushPromises()
    expect(pluginStore.toggleActivation).toHaveBeenCalledWith('team', true)

    await admin.get('[data-testid="detail-back"]').trigger('click')
    await openDetail(admin, 'mine')
    expect(admin.find('[data-testid="plugin-toggle-mine"]').exists()).toBe(false)
  })

  it('lets admins withdraw a released plugin but not release an unpublished one', async () => {
    auth.isAdmin = true
    pluginStore.plugins = [
      plugin(),
      plugin({ id: 'draft', name: 'Draft', published: false, org_enabled: false }),
    ]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'p-1')
    expect(wrapper.getComponent(DetailHeader).text()).toContain('Available to members')
    const toggle = wrapper.get('[data-testid="plugin-toggle-p-1"]')
    expect(toggle.attributes('disabled')).toBeUndefined()
    await toggle.trigger('click')
    await flushPromises()
    expect(pluginStore.toggleActivation).toHaveBeenCalledWith('p-1', false)

    await wrapper.get('[data-testid="detail-back"]').trigger('click')
    await openDetail(wrapper, 'draft')
    expect(wrapper.get('[data-testid="plugin-toggle-draft"]').attributes('disabled')).toBeDefined()
  })

  it('offers Edit and Delete only for plugins the user may change, regardless of role', async () => {
    pluginStore.plugins = [plugin(), shared(), personal()]
    auth.isAdmin = true
    const admin = mountPanel()
    await openDetail(admin, 'p-1')
    expect(admin.find('[data-testid="plugin-edit-detail"]').exists()).toBe(false)
    expect(admin.find('[data-testid="plugin-delete-detail"]').exists()).toBe(false)
    admin.unmount()

    auth.isAdmin = false
    const member = mountPanel()
    await openDetail(member, 'team')
    expect(member.find('[data-testid="plugin-edit-detail"]').exists()).toBe(false)
    expect(member.find('[data-testid="plugin-delete-detail"]').exists()).toBe(false)

    await member.get('[data-testid="detail-back"]').trigger('click')
    await openDetail(member, 'mine')
    await member.get('[data-testid="plugin-edit-detail"]').trigger('click')
    expect(member.getComponent(EditorStub).props()).toMatchObject({
      open: true,
      plugin: expect.objectContaining({ id: 'mine' }),
    })
    expect(member.find('[data-testid="plugin-delete-detail"]').exists()).toBe(true)
  })

  it('deletes after confirmation and returns to the catalog', async () => {
    pluginStore.plugins = [personal()]
    const wrapper = mountPanel()
    await openDetail(wrapper, 'mine')
    expect(wrapper.find('[data-testid="plugin-delete-confirm"]').exists()).toBe(false)
    await wrapper.get('[data-testid="plugin-delete-detail"]').trigger('click')
    expect(wrapper.text()).toContain('Delete My tools?')
    routerReplace.mockClear()
    await wrapper.get('[data-testid="plugin-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(pluginStore.deletePlugin).toHaveBeenCalledWith('mine')
    expect(routerReplace).toHaveBeenCalledWith({ path: '/', query: {} })
    expect(wrapper.find('[data-testid="plugin-delete-confirm"]').exists()).toBe(false)
  })
})
