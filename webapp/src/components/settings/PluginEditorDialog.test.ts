import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import PluginEditorDialog from './PluginEditorDialog.vue'
import { Select, SelectItem } from '@/components/ui/select'
import type { Credential } from '@/types'
import type {
  CredentialService,
  Plugin,
  PluginCreateIn,
  PluginCredentialRequirement,
  PluginMcpServer,
  PluginUpdateIn,
} from '@opencuria/chat-core/plugins'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const pluginStoreMock = vi.hoisted(() => ({
  createPlugin: vi.fn(),
  updatePlugin: vi.fn(),
  lastError: null as string | null,
}))
vi.mock('@/stores/plugins', () => ({ usePluginStore: () => pluginStoreMock }))

const authMock = vi.hoisted(() => ({ isAdmin: false, activeOrganizationId: 'org-1' }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => authMock }))

function service(overrides: Partial<CredentialService>): CredentialService {
  return {
    id: 'svc-1',
    name: 'Playwright Auth',
    slug: 'playwright-auth',
    description: '',
    credential_type: 'env',
    env_var_name: 'PLAYWRIGHT_TOKEN',
    target_path: '',
    label: '',
    organization_id: 'org-1',
    oauth_server_url: '',
    is_active: true,
    ...overrides,
  }
}

function credential(overrides: Partial<Credential>): Credential {
  return {
    id: 'cred-1',
    name: 'Work token',
    scope: 'personal',
    service_id: 'svc-1',
    service_name: 'Playwright Auth',
    service_slug: 'playwright-auth',
    credential_type: 'env',
    env_var_name: 'PLAYWRIGHT_TOKEN',
    target_path: '',
    has_public_key: false,
    created_by_id: 1,
    created_at: '',
    updated_at: '',
    oauth_connected: false,
    oauth_status: '',
    oauth_reconnect_required: false,
    oauth_expires_at: null,
    ...overrides,
  }
}

const credentialStoreMock = vi.hoisted(() => ({
  credentials: [] as Credential[],
  servicesLoaded: true,
  servicesError: null,
  services: [] as CredentialService[],
  loading: false,
  error: null,
  fetchCredentials: vi.fn(),
  fetchServices: vi.fn(),
}))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => credentialStoreMock }))

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>', props: ['open'] },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

function server(overrides: Partial<PluginMcpServer> = {}): PluginMcpServer {
  return {
    id: 'm-1',
    name: 'Acme',
    slug: 'acme',
    transport: 'stdio',
    command: 'npx',
    args: ['-y', '@acme/mcp'],
    cwd: '/workspace',
    env: { LOG_LEVEL: 'debug' },
    url: '',
    headers: {},
    auth_type: 'none',
    oauth_requirement_key: '',
    ...overrides,
  }
}

function requirement(
  overrides: Partial<PluginCredentialRequirement> = {},
): PluginCredentialRequirement {
  return {
    id: 'r-1',
    key: 'api_key',
    description: '',
    required: true,
    service_id: 'svc-1',
    service_name: 'Playwright Auth',
    service_slug: 'playwright-auth',
    credential_type: 'env',
    credential_id: null,
    credential_name: '',
    credential_scope: '',
    ...overrides,
  }
}

/** The requirement behind a secret variable `{{credential.<key>}}`. */
function secretRequirement(
  key: string,
  serviceId: string,
  credentialId: string | null,
  credentialName = '',
): PluginCredentialRequirement {
  return requirement({
    id: `r-${key}`,
    key,
    service_id: serviceId,
    service_name: `${key.toUpperCase()} (Playwright)`,
    service_slug: serviceId,
    credential_id: credentialId,
    credential_name: credentialName,
    credential_scope: credentialId ? 'personal' : '',
  })
}

function makePlugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: 'plugin-1',
    name: 'Playwright',
    slug: 'playwright',
    description: 'Browser automation',
    scope: 'personal',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: false,
    org_enabled: false,
    available: true,
    editable: true,
    skills: [
      {
        id: 's-1',
        name: 'Basics',
        slug: 'basics',
        description: 'Browser basics.',
        body: 'Use it.',
        position: 0,
      },
    ],
    mcp_servers: [],
    credential_requirements: [requirement()],
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

/**
 * Renders the "Where X comes from" menus of secret variables inline, so a
 * test can pick an entry without opening a floating menu.
 */
const menuStubs = {
  DropdownMenu: { template: '<div data-stub="dropdown-menu"><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuLabel: { template: '<div data-stub="dropdown-menu-label"><slot /></div>' },
  DropdownMenuSeparator: { template: '<hr />' },
  DropdownMenuItem: {
    emits: ['select'],
    template: '<button type="button" role="menuitem" @click="$emit(\'select\', $event)"><slot /></button>',
  },
}

function mountEditor(
  plugin: Plugin | null,
  stubs: Record<string, object> = {},
): VueWrapper {
  setActivePinia(createPinia())
  return mount(PluginEditorDialog, {
    props: { open: true, plugin },
    global: { stubs: { ...dialogStubs, ...stubs } },
  })
}

async function save(wrapper: VueWrapper): Promise<void> {
  await wrapper.get('#plugin-editor-form').trigger('submit')
  await flushPromises()
}

function created(): PluginCreateIn {
  expect(pluginStoreMock.createPlugin).toHaveBeenCalledTimes(1)
  return pluginStoreMock.createPlugin.mock.calls[0]![0]
}

function updated(): PluginUpdateIn {
  expect(pluginStoreMock.updatePlugin).toHaveBeenCalledTimes(1)
  return pluginStoreMock.updatePlugin.mock.calls[0]![1]
}

function mcp(wrapper: VueWrapper, index = 0) {
  return wrapper.get(`[data-testid="plugin-mcp-${index}"]`)
}

/** Adds the first MCP server as a command with a name and a command line. */
async function addCommandServer(wrapper: VueWrapper, name: string, commandLine: string) {
  await wrapper.get('[data-testid="plugin-add-mcp"]').trigger('click')
  await mcp(wrapper).get('[id^="mcp-name-"]').setValue(name)
  await wrapper.get('[data-testid="plugin-mcp-command-0"]').setValue(commandLine)
}

async function choose(wrapper: VueWrapper, testId: string, value: string): Promise<void> {
  const select = wrapper
    .findAllComponents(Select)
    .find((candidate) => candidate.find(`[data-testid="${testId}"]`).exists())
  expect(select, testId).toBeDefined()
  select!.vm.$emit('update:modelValue', value)
  await flushPromises()
}

/** Opens Advanced and adds a per-user credential requirement. */
async function addRequirement(wrapper: VueWrapper): Promise<void> {
  expect(wrapper.find('[data-testid="plugin-add-requirement"]').exists()).toBe(false)
  await wrapper.get('[data-testid="plugin-advanced"]').trigger('click')
  await wrapper.get('[data-testid="plugin-add-requirement"]').trigger('click')
}

/** The source menu of variable `index` (0-based) of the first server. */
function menuOf(wrapper: VueWrapper, index: number): Element {
  const menu = mcp(wrapper)
    .get(`[data-testid="plugin-variable-menu-${index}"]`)
    .element.closest('[data-stub="dropdown-menu"]')
  expect(menu, `menu of variable ${index + 1}`).toBeTruthy()
  return menu!
}

/** Entries of the source menu of variable `index`. */
function variableMenu(wrapper: VueWrapper, index: number) {
  const menu = menuOf(wrapper, index)
  return mcp(wrapper)
    .findAll('[role="menuitem"]')
    .filter((item) => item.element.closest('[data-stub="dropdown-menu"]') === menu)
}

/** What an entry is called: a credential's name without its service, or the entry text. */
function entryName(item: DOMWrapper<Element>): string {
  const innermost = item.findAll('span').find((span) => !span.find('span').exists())
  return (innermost ?? item).text()
}

/** Credentials the source menu of variable `index` offers under "Use a credential". */
function offeredCredentials(wrapper: VueWrapper, index: number): string[] {
  const label = menuOf(wrapper, index).querySelector('[data-stub="dropdown-menu-label"]')
  if (!label) return []
  expect(label.textContent?.trim()).toBe('Use a credential')
  return variableMenu(wrapper, index)
    .filter((item) => label.compareDocumentPosition(item.element) & Node.DOCUMENT_POSITION_FOLLOWING)
    .map(entryName)
}

async function chooseSource(wrapper: VueWrapper, index: number, name: string): Promise<void> {
  const item = variableMenu(wrapper, index).find((entry) => entryName(entry) === name)
  expect(item, name).toBeDefined()
  await item!.trigger('click')
}

/** Adds a variable to the first server and names it. */
async function addVariable(wrapper: VueWrapper, name: string): Promise<void> {
  await wrapper.get('[data-testid="plugin-mcp-add-variable-0"]').trigger('click')
  const count = mcp(wrapper).findAll('[aria-label$=" name"][aria-label^="Variable "]').length
  await mcp(wrapper).get(`[aria-label="Variable ${count} name"]`).setValue(name)
}

async function selectTab(wrapper: VueWrapper, testId: string): Promise<void> {
  await wrapper.get(`[data-testid="${testId}"]`).trigger('mousedown')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  authMock.isAdmin = false
  pluginStoreMock.lastError = null
  pluginStoreMock.createPlugin.mockImplementation(async (data: PluginCreateIn) => ({
    ...makePlugin(),
    id: 'new',
    name: data.name,
  }))
  pluginStoreMock.updatePlugin.mockImplementation(async () => makePlugin())
  credentialStoreMock.credentials = []
  credentialStoreMock.servicesLoaded = true
  credentialStoreMock.services = [
    service({}),
    service({
      id: 'oauth-service',
      name: 'Notion OAuth',
      slug: 'notion-oauth',
      credential_type: 'mcp_oauth',
      env_var_name: '',
      oauth_server_url: 'https://mcp.notion.com/mcp',
    }),
  ]
})

describe('PluginEditorDialog', () => {
  it('keeps edits local until Save and discards cancelled drafts', async () => {
    vi.useFakeTimers()
    const wrapper = mountEditor(makePlugin())
    try {
      await wrapper.get('[data-testid="plugin-name"]').setValue('Draft name')
      await vi.advanceTimersByTimeAsync(1000)
      expect(pluginStoreMock.updatePlugin).not.toHaveBeenCalled()
      await wrapper
        .findAll('button')
        .find((button) => button.text() === 'Cancel')!
        .trigger('click')
      expect(wrapper.emitted('update:open')).toEqual([[false]])
      wrapper.unmount()
      expect(pluginStoreMock.updatePlugin).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('shows the backend failure and retains the edit for another Save attempt', async () => {
    pluginStoreMock.updatePlugin.mockImplementationOnce(async () => {
      pluginStoreMock.lastError = 'A plugin with this name already exists.'
      return null
    })
    const wrapper = mountEditor(makePlugin())
    await wrapper.get('[data-testid="plugin-name"]').setValue('Updated plugin')
    await save(wrapper)
    expect(wrapper.get('[data-testid="plugin-editor-error"]').text()).toBe(
      'A plugin with this name already exists.',
    )
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(
      (wrapper.get('[data-testid="plugin-name"]').element as HTMLInputElement).value,
    ).toBe('Updated plugin')

    await save(wrapper)
    expect(pluginStoreMock.updatePlugin).toHaveBeenCalledTimes(2)
    expect(pluginStoreMock.updatePlugin.mock.calls[1]![1]).toMatchObject({
      name: 'Updated plugin',
    })
    expect(wrapper.emitted('update:open')).toEqual([[false]])
    expect(wrapper.emitted('saved')).toHaveLength(1)
  })

  it('falls back to a generic message when the store has no error text', async () => {
    pluginStoreMock.createPlugin.mockResolvedValueOnce(null)
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Demo')
    await save(wrapper)
    expect(wrapper.get('[data-testid="plugin-editor-error"]').text()).toBe(
      'The plugin could not be saved.',
    )
  })

  it('shows validation only after the first save attempt', async () => {
    const wrapper = mountEditor(null)
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-editor-validation"]').exists()).toBe(false)
    expect(
      wrapper.get('[data-testid="plugin-editor-save"]').attributes('disabled'),
    ).toBeUndefined()

    await save(wrapper)
    expect(pluginStoreMock.createPlugin).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="plugin-editor-validation"]').text()).toContain(
      'Enter a plugin name.',
    )

    // Once shown, errors follow the edits.
    await wrapper.get('[data-testid="plugin-name"]').setValue('Demo')
    expect(wrapper.find('[data-testid="plugin-editor-validation"]').exists()).toBe(false)
    await save(wrapper)
    expect(created()).toMatchObject({ name: 'Demo', scope: 'personal' })
    expect(wrapper.emitted('saved')?.[0]?.[0]).toMatchObject({ id: 'new' })
  })

  it('reopens entries with errors on save and shows their errors inside', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Demo')
    await wrapper.get('[data-testid="plugin-add-mcp"]').trigger('click')
    // A fresh entry shows no errors until Save, then collapse it.
    expect(mcp(wrapper).text()).not.toContain('Enter a name.')
    await mcp(wrapper).get('button[aria-expanded="true"]').trigger('click')
    expect(mcp(wrapper).find('[id^="mcp-name-"]').exists()).toBe(false)

    await save(wrapper)
    expect(pluginStoreMock.createPlugin).not.toHaveBeenCalled()
    expect(mcp(wrapper).find('[id^="mcp-name-"]').exists()).toBe(true)
    expect(mcp(wrapper).text()).toContain('Enter a name.')
    expect(mcp(wrapper).text()).toContain('Enter the command that starts the server.')
    // Entry errors stay with the entry.
    expect(wrapper.find('[data-testid="plugin-editor-validation"]').exists()).toBe(false)
  })

  it('sends a command line as command and args with empty URL fields', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Demo Plugin')
    expect(wrapper.find('[data-testid="plugin-slug"]').exists()).toBe(false)
    await addCommandServer(wrapper, 'Playwright', `npx -y @playwright/mcp@latest --caps 'a b'`)
    await save(wrapper)

    const payload = created()
    expect(payload).toMatchObject({ name: 'Demo Plugin', slug: '', scope: 'personal' })
    expect(payload.mcp_servers).toEqual([
      expect.objectContaining({
        name: 'Playwright',
        slug: '',
        transport: 'stdio',
        command: 'npx',
        args: ['-y', '@playwright/mcp@latest', '--caps', 'a b'],
        env: {},
        url: '',
        headers: {},
        auth_type: 'none',
      }),
    ])
    // Servers carry no secrets of their own; there are no credentials either.
    expect(payload.mcp_servers?.[0]).not.toHaveProperty('secrets')
    expect(payload.credential_requirements).toEqual([])
  })

  it('stores secret variables as credential placeholders and sends their values as credentials', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Acme')
    await addCommandServer(wrapper, 'Acme', 'npx -y @acme/mcp')
    const server = mcp(wrapper)

    await addVariable(wrapper, 'API_TOKEN')
    // Names like API_TOKEN become secrets by default.
    expect(server.find('[aria-label="Variable 1 value"]').exists()).toBe(false)
    const secretValue = server.get('[aria-label="Variable 1 secret value"]')
    expect(secretValue.attributes('type')).toBe('password')
    expect(secretValue.attributes('placeholder')).toBe('Value')
    await secretValue.setValue('sk-live-123')

    await addVariable(wrapper, 'LOG_LEVEL')
    expect(server.find('[aria-label="Variable 2 secret value"]').exists()).toBe(false)
    const plainValue = server.get('[aria-label="Variable 2 value"]')
    expect(plainValue.attributes('type')).not.toBe('password')
    await plainValue.setValue('debug')

    await addVariable(wrapper, 'WORKSPACE_ID')
    await server.get('[aria-label="Variable 3 value"]').setValue('ws-9')
    const toggles = server.findAll('button[aria-pressed]')
    expect(toggles.map((toggle) => toggle.attributes('aria-pressed'))).toEqual([
      'true',
      'false',
      'false',
    ])
    // Making a plain variable secret keeps the typed value as the secret.
    await toggles[2]!.trigger('click')
    expect(server.get('[aria-label="WORKSPACE_ID is secret"]').attributes('aria-pressed')).toBe(
      'true',
    )
    expect(
      (server.get('[aria-label="Variable 3 secret value"]').element as HTMLInputElement).value,
    ).toBe('ws-9')

    await save(wrapper)
    const payload = created()
    expect(payload.mcp_servers?.[0]?.env).toEqual({
      API_TOKEN: '{{credential.api_token}}',
      LOG_LEVEL: 'debug',
      WORKSPACE_ID: '{{credential.workspace_id}}',
    })
    // Each secret becomes a new credential service of the plugin with its value.
    expect(payload.credential_requirements).toEqual([
      {
        key: 'api_token',
        required: true,
        new_service: { name: 'API_TOKEN (Acme)', env_var_name: 'API_TOKEN' },
        credential_id: null,
        value: 'sk-live-123',
      },
      {
        key: 'workspace_id',
        required: true,
        new_service: { name: 'WORKSPACE_ID (Acme)', env_var_name: 'WORKSPACE_ID' },
        credential_id: null,
        value: 'ws-9',
      },
    ])
    // Secret values never end up in the server configuration.
    expect(JSON.stringify(payload.mcp_servers)).not.toContain('sk-live-123')
    expect(JSON.stringify(payload.mcp_servers)).not.toContain('ws-9')
  })

  it('keeps a saved secret left empty, replaces a typed one and drops removed ones', async () => {
    const wrapper = mountEditor(
      makePlugin({
        mcp_servers: [
          server({
            env: {
              LOG_LEVEL: 'debug',
              API_TOKEN: '{{credential.api_token}}',
              OTHER_KEY: '{{credential.other_key}}',
              OLD_KEY: '{{credential.old_key}}',
              TEAM_TOKEN: '{{credential.team_token}}',
            },
          }),
        ],
        credential_requirements: [
          secretRequirement('api_token', 'svc-api', 'cred-api', 'Playwright: API_TOKEN'),
          secretRequirement('other_key', 'svc-other', 'cred-other', 'Playwright: OTHER_KEY'),
          secretRequirement('old_key', 'svc-old', 'cred-old', 'Playwright: OLD_KEY'),
          secretRequirement('team_token', 'svc-team', null),
        ],
      }),
    )
    // Requirements of secret variables belong to the variables, not to "More credentials".
    expect(wrapper.find('[data-testid="plugin-requirement-0"]').exists()).toBe(false)
    await wrapper.get('[data-testid="plugin-mcp-0"] button[aria-expanded="false"]').trigger('click')
    const row = mcp(wrapper)
    // Saved secrets never come back; the placeholder names their credential.
    const savedValue = row.get('[aria-label="Variable 2 secret value"]')
    expect((savedValue.element as HTMLInputElement).value).toBe('')
    expect(savedValue.attributes('type')).toBe('password')
    expect(savedValue.attributes('placeholder')).toBe('Saved · Playwright: API_TOKEN')
    // A secret without a credential of the plugin is left to each workspace.
    expect(row.find('[aria-label="Variable 5 secret value"]').exists()).toBe(false)
    expect(row.get('[data-testid="plugin-variable-source-4"]').text()).toBe(
      'Each workspace adds its own',
    )

    // Renaming a saved secret keeps its credential.
    await row.get('[aria-label="Variable 2 name"]').setValue('ACME_TOKEN')
    await row.get('[aria-label="Variable 3 secret value"]').setValue('rotated')
    await row.get('[aria-label="Remove OLD_KEY"]').trigger('click')
    await save(wrapper)

    expect(pluginStoreMock.updatePlugin.mock.calls[0]![0]).toBe('plugin-1')
    const payload = updated()
    expect(payload.mcp_servers?.[0]).toMatchObject({
      id: 'm-1',
      command: 'npx',
      args: ['-y', '@acme/mcp'],
    })
    expect(payload.mcp_servers?.[0]?.env).toEqual({
      LOG_LEVEL: 'debug',
      ACME_TOKEN: '{{credential.api_token}}',
      OTHER_KEY: '{{credential.other_key}}',
      TEAM_TOKEN: '{{credential.team_token}}',
    })
    expect(payload.credential_requirements).toEqual([
      // Left empty: no value, the saved credential stays.
      { key: 'api_token', required: true, service_id: 'svc-api', credential_id: 'cred-api' },
      {
        key: 'other_key',
        required: true,
        service_id: 'svc-other',
        credential_id: 'cred-other',
        value: 'rotated',
      },
      { key: 'team_token', required: true, service_id: 'svc-team', credential_id: null },
    ])
  })

  it('asks for the value of a new secret before saving', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Acme')
    await addCommandServer(wrapper, 'Acme', 'npx -y @acme/mcp')
    await wrapper.get('[data-testid="plugin-mcp-add-variable-0"]').trigger('click')
    await mcp(wrapper).get('[aria-label="Variable 1 name"]').setValue('API_TOKEN')
    await save(wrapper)
    expect(pluginStoreMock.createPlugin).not.toHaveBeenCalled()
    expect(mcp(wrapper).text()).toContain('Enter the secret value of "API_TOKEN".')
  })

  it('adds servers from a pasted mcpServers configuration', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Dev tools')
    expect(wrapper.find('[data-testid="plugin-import-text"]').exists()).toBe(false)
    await wrapper.get('[data-testid="plugin-import-mcp"]').trigger('click')
    expect(
      wrapper.get('[data-testid="plugin-import-apply"]').attributes('disabled'),
    ).toBeDefined()

    await wrapper.get('[data-testid="plugin-import-text"]').setValue(
      JSON.stringify({
        mcpServers: {
          github: {
            command: 'npx',
            args: ['-y', '@modelcontextprotocol/server-github'],
            env: { GITHUB_PERSONAL_ACCESS_TOKEN: '<YOUR_TOKEN>', LOG_LEVEL: 'info' },
          },
          linear: { url: 'https://mcp.linear.app/sse' },
        },
      }),
    )
    await wrapper.get('[data-testid="plugin-import-apply"]').trigger('click')
    expect(wrapper.find('[data-testid="plugin-import-panel"]').exists()).toBe(false)
    expect(
      (mcp(wrapper, 0).get('[id^="mcp-name-"]').element as HTMLInputElement).value,
    ).toBe('Github')
    expect(
      (wrapper.get('[data-testid="plugin-mcp-command-0"]').element as HTMLInputElement).value,
    ).toBe('npx -y @modelcontextprotocol/server-github')
    expect(
      (wrapper.get('[data-testid="plugin-mcp-url-1"]').element as HTMLInputElement).value,
    ).toBe('https://mcp.linear.app/sse')

    // The placeholder token becomes an empty secret to fill in.
    const token = mcp(wrapper, 0).get('[aria-label="Variable 1 secret value"]')
    expect(token.attributes('type')).toBe('password')
    expect((token.element as HTMLInputElement).value).toBe('')
    expect(
      (mcp(wrapper, 0).get('[aria-label="Variable 2 value"]').element as HTMLInputElement).value,
    ).toBe('info')
    await token.setValue('ghp_secret')
    await save(wrapper)

    const payload = created()
    expect(payload.credential_requirements).toEqual([
      {
        key: 'github_personal_access_token',
        required: true,
        new_service: {
          name: 'GITHUB_PERSONAL_ACCESS_TOKEN (Dev tools)',
          env_var_name: 'GITHUB_PERSONAL_ACCESS_TOKEN',
        },
        credential_id: null,
        value: 'ghp_secret',
      },
    ])
    expect(JSON.stringify(payload)).not.toContain('<YOUR_TOKEN>')
    expect(payload.mcp_servers).toEqual([
      expect.objectContaining({
        name: 'Github',
        transport: 'stdio',
        command: 'npx',
        args: ['-y', '@modelcontextprotocol/server-github'],
        env: {
          GITHUB_PERSONAL_ACCESS_TOKEN: '{{credential.github_personal_access_token}}',
          LOG_LEVEL: 'info',
        },
      }),
      expect.objectContaining({
        name: 'Linear',
        transport: 'sse',
        url: 'https://mcp.linear.app/sse',
        command: '',
        args: [],
      }),
    ])
  })

  it('explains a pasted configuration it cannot read and adds nothing', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-import-mcp"]').trigger('click')
    await wrapper.get('[data-testid="plugin-import-text"]').setValue('{"mcpServers": ')
    await wrapper.get('[data-testid="plugin-import-apply"]').trigger('click')
    expect(wrapper.get('[data-testid="plugin-import-panel"] [role="alert"]').text()).toBe(
      'This is not valid JSON.',
    )
    expect(wrapper.find('[data-testid="plugin-mcp-0"]').exists()).toBe(false)
  })

  it('lets admins share a new plugin with the organization', async () => {
    authMock.isAdmin = true
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Team tools')
    expect(
      wrapper.get('[data-testid="plugin-scope-personal"]').attributes('data-state'),
    ).toBe('active')
    await selectTab(wrapper, 'plugin-scope-organization')
    await save(wrapper)
    expect(created()).toMatchObject({ name: 'Team tools', scope: 'organization' })
  })

  it('never offers members the organization scope', async () => {
    const wrapper = mountEditor(null)
    expect(wrapper.text()).not.toContain('Who can use it')
    expect(wrapper.find('[data-testid="plugin-scope-organization"]').exists()).toBe(false)
    await wrapper.get('[data-testid="plugin-name"]').setValue('My tools')
    await save(wrapper)
    expect(created().scope).toBe('personal')
  })

  it('does not offer to move an existing plugin to another scope', async () => {
    authMock.isAdmin = true
    const wrapper = mountEditor(makePlugin())
    expect(wrapper.find('[data-testid="plugin-scope-organization"]').exists()).toBe(false)
    await save(wrapper)
    expect(updated()).not.toHaveProperty('scope')
  })

  it('fills name and description from a pasted SKILL.md', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Writing')
    await wrapper.get('[data-testid="plugin-add-skill"]').trigger('click')
    const skill = wrapper.get('[data-testid="plugin-skill-0"]')
    await skill
      .get('[id^="skill-body-"]')
      .setValue('---\nname: release-notes\ndescription: Write release notes.\n---\n# Steps\nDo it.')
    expect((skill.get('[id^="skill-name-"]').element as HTMLInputElement).value).toBe(
      'Release notes',
    )
    expect(
      (skill.get('[id^="skill-description-"]').element as HTMLTextAreaElement).value,
    ).toBe('Write release notes.')
    await save(wrapper)
    expect(created().skills).toEqual([
      {
        name: 'Release notes',
        slug: '',
        description: 'Write release notes.',
        body: '# Steps\nDo it.',
        position: 0,
      },
    ])
  })

  it('round-trips an existing plugin and keeps ids and settings it does not edit', async () => {
    const wrapper = mountEditor(
      makePlugin({
        mcp_servers: [
          server({
            cwd: '/opt/acme',
            resources: { desktop: { activation: 'first_tool' } },
            tool_policy: { exposed_tools: ['browse'] },
          }),
        ],
      }),
    )
    // Nothing is expanded for an existing plugin; requirements open Advanced.
    expect(wrapper.find('[id^="skill-name-"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="plugin-requirement-0"]').exists()).toBe(true)
    await save(wrapper)

    expect(updated()).toMatchObject({
      name: 'Playwright',
      description: 'Browser automation',
      skills: [
        { id: 's-1', name: 'Basics', description: 'Browser basics.', body: 'Use it.', position: 0 },
      ],
      mcp_servers: [
        {
          id: 'm-1',
          cwd: '/opt/acme',
          resources: { desktop: { activation: 'first_tool' } },
          tool_policy: { exposed_tools: ['browse'] },
        },
      ],
      credential_requirements: [
        { key: 'api_key', description: '', required: true, service_id: 'svc-1', credential_id: null },
      ],
    })
  })

  it('switching a server to URL clears what a command used', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Remote')
    await addCommandServer(wrapper, 'Remote', 'npx -y @acme/mcp')
    await wrapper.get('[data-testid="plugin-mcp-add-variable-0"]').trigger('click')
    await mcp(wrapper).get('[aria-label="Variable 1 name"]').setValue('LOG_LEVEL')

    await selectTab(wrapper, 'plugin-mcp-kind-url-0')
    expect(wrapper.find('[data-testid="plugin-mcp-command-0"]').exists()).toBe(false)
    expect(mcp(wrapper).find('[aria-label="Header 1 name"]').exists()).toBe(false)
    await wrapper.get('[data-testid="plugin-mcp-url-0"]').setValue('https://mcp.example.com/mcp')
    await save(wrapper)
    expect(created().mcp_servers?.[0]).toMatchObject({
      transport: 'streamable_http',
      url: 'https://mcp.example.com/mcp',
      command: '',
      args: [],
      env: {},
      headers: {},
    })
  })
})

describe('PluginEditorDialog credentials', () => {
  type EditorForm = ReturnType<
    (typeof import('@opencuria/chat-core/pluginForms'))['emptyPluginForm']
  >

  function editorForm(wrapper: VueWrapper): EditorForm {
    return (wrapper.vm as unknown as { form: EditorForm }).form
  }

  it('links a secret variable to an existing credential and sends its service', async () => {
    credentialStoreMock.credentials = [
      credential({
        id: 'cred-gh',
        name: 'Work GitHub',
        service_id: 'svc-gh',
        service_name: 'GitHub',
      }),
      credential({
        id: 'cred-ssh',
        name: 'Deploy key',
        service_id: 'svc-ssh',
        service_name: 'SSH',
        credential_type: 'ssh_key',
      }),
      credential({
        id: 'cred-notion',
        name: 'Notion sign-in',
        service_id: 'oauth-service',
        service_name: 'Notion OAuth',
        credential_type: 'mcp_oauth',
      }),
    ]
    const wrapper = mountEditor(null, menuStubs)
    expect(credentialStoreMock.fetchCredentials).toHaveBeenCalled()
    await wrapper.get('[data-testid="plugin-name"]').setValue('GitHub tools')
    await addCommandServer(wrapper, 'GitHub', 'npx -y @modelcontextprotocol/server-github')
    await addVariable(wrapper, 'GITHUB_TOKEN')
    expect(
      mcp(wrapper).get('[data-testid="plugin-variable-menu-0"]').attributes('aria-label'),
    ).toBe('Where GITHUB_TOKEN comes from')
    // Only credentials that hold a value can fill a variable.
    expect(offeredCredentials(wrapper, 0)).toEqual(['Work GitHub'])

    await chooseSource(wrapper, 0, 'Work GitHub')
    const row = mcp(wrapper)
    expect(row.find('[aria-label="Variable 1 secret value"]').exists()).toBe(false)
    expect(row.get('[data-testid="plugin-variable-source-0"]').text()).toBe('Work GitHub')
    await save(wrapper)

    const payload = created()
    expect(payload.mcp_servers?.[0]?.env).toEqual({
      GITHUB_TOKEN: '{{credential.github_token}}',
    })
    // The plugin uses the credential as it is: no value, no new service.
    expect(payload.credential_requirements).toEqual([
      { key: 'github_token', required: true, service_id: 'svc-gh', credential_id: 'cred-gh' },
    ])
  })

  it('switches a saved secret to another existing credential', async () => {
    credentialStoreMock.credentials = [
      credential({ id: 'cred-api', name: 'Playwright: API_TOKEN', service_id: 'svc-api' }),
      credential({ id: 'cred-team', name: 'Team token', service_id: 'svc-team' }),
    ]
    const wrapper = mountEditor(
      makePlugin({
        mcp_servers: [server({ env: { API_TOKEN: '{{credential.api_token}}' } })],
        credential_requirements: [
          secretRequirement('api_token', 'svc-api', 'cred-api', 'Playwright: API_TOKEN'),
        ],
      }),
      menuStubs,
    )
    await wrapper.get('[data-testid="plugin-mcp-0"] button[aria-expanded="false"]').trigger('click')
    // The saved credential is the current value, not another choice.
    expect(offeredCredentials(wrapper, 0)).toEqual(['Team token'])

    await chooseSource(wrapper, 0, 'Team token')
    expect(mcp(wrapper).get('[data-testid="plugin-variable-source-0"]').text()).toBe('Team token')
    await save(wrapper)
    const payload = updated()
    expect(payload.mcp_servers?.[0]?.env).toEqual({ API_TOKEN: '{{credential.api_token}}' })
    expect(payload.credential_requirements).toEqual([
      { key: 'api_token', required: true, service_id: 'svc-team', credential_id: 'cred-team' },
    ])
  })

  it('leaves a secret variable to each workspace without sending a value', async () => {
    credentialStoreMock.credentials = [credential({ id: 'cred-gh', name: 'Work GitHub' })]
    const wrapper = mountEditor(null, menuStubs)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Acme')
    await addCommandServer(wrapper, 'Acme', 'npx -y @acme/mcp')
    await addVariable(wrapper, 'API_TOKEN')
    await mcp(wrapper).get('[aria-label="Variable 1 secret value"]').setValue('typed-first')

    await chooseSource(wrapper, 0, 'Each workspace adds its own')
    expect(mcp(wrapper).find('[aria-label="Variable 1 secret value"]').exists()).toBe(false)
    expect(mcp(wrapper).get('[data-testid="plugin-variable-source-0"]').text()).toBe(
      'Each workspace adds its own',
    )
    await save(wrapper)

    const payload = created()
    expect(payload.mcp_servers?.[0]?.env).toEqual({ API_TOKEN: '{{credential.api_token}}' })
    expect(payload.credential_requirements).toEqual([
      {
        key: 'api_token',
        required: true,
        new_service: { name: 'API_TOKEN (Acme)', env_var_name: 'API_TOKEN' },
        credential_id: null,
      },
    ])
    expect(JSON.stringify(payload)).not.toContain('typed-first')
  })

  it('asks for a value again after switching a linked secret back to "Enter a value"', async () => {
    credentialStoreMock.credentials = [credential({ id: 'cred-gh', name: 'Work GitHub' })]
    const wrapper = mountEditor(null, menuStubs)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Acme')
    await addCommandServer(wrapper, 'Acme', 'npx -y @acme/mcp')
    await addVariable(wrapper, 'API_TOKEN')
    await chooseSource(wrapper, 0, 'Work GitHub')
    await chooseSource(wrapper, 0, 'Enter a value')

    const value = mcp(wrapper).get('[aria-label="Variable 1 secret value"]')
    expect(value.attributes('placeholder')).toBe('Value')
    await save(wrapper)
    expect(pluginStoreMock.createPlugin).not.toHaveBeenCalled()
    expect(mcp(wrapper).text()).toContain('Enter the secret value of "API_TOKEN".')
  })

  it('offers organization plugins only organization credentials', async () => {
    authMock.isAdmin = true
    credentialStoreMock.credentials = [
      credential({ id: 'mine', name: 'My GitHub', scope: 'personal' }),
      credential({ id: 'team', name: 'Team GitHub', scope: 'organization' }),
    ]
    const wrapper = mountEditor(null, menuStubs)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Team tools')
    await addCommandServer(wrapper, 'GitHub', 'npx -y @modelcontextprotocol/server-github')
    await addVariable(wrapper, 'GITHUB_TOKEN')
    expect(offeredCredentials(wrapper, 0)).toEqual(['My GitHub', 'Team GitHub'])

    await selectTab(wrapper, 'plugin-scope-organization')
    expect(offeredCredentials(wrapper, 0)).toEqual(['Team GitHub'])
  })

  it('drops a personal credential when a new plugin is shared with the organization', async () => {
    authMock.isAdmin = true
    credentialStoreMock.credentials = [
      credential({ id: 'mine', name: 'My GitHub', scope: 'personal' }),
      credential({ id: 'team', name: 'Team GitHub', scope: 'organization' }),
    ]
    const wrapper = mountEditor(null, menuStubs)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Team tools')
    await addCommandServer(wrapper, 'GitHub', 'npx -y @modelcontextprotocol/server-github')
    await addVariable(wrapper, 'GITHUB_TOKEN')
    await chooseSource(wrapper, 0, 'My GitHub')
    expect(mcp(wrapper).get('[data-testid="plugin-variable-source-0"]').text()).toBe('My GitHub')

    await selectTab(wrapper, 'plugin-scope-organization')
    // The secret asks for a value again instead of failing on save.
    expect(mcp(wrapper).find('[data-testid="plugin-variable-source-0"]').exists()).toBe(false)
    expect(mcp(wrapper).find('[aria-label="Variable 1 secret value"]').exists()).toBe(true)
  })

  it('lets a further credential always use one credential of its service', async () => {
    credentialStoreMock.credentials = [
      credential({ id: 'cred-pw', name: 'Work Playwright' }),
      credential({ id: 'cred-gh', name: 'Work GitHub', service_id: 'svc-gh', service_name: 'GitHub' }),
    ]
    const wrapper = mountEditor(makePlugin())
    await flushPromises()
    const select = wrapper
      .findAllComponents(Select)
      .find((entry) => entry.find('[data-testid="plugin-requirement-credential-0"]').exists())!
    expect(select.props('modelValue')).not.toBe('cred-pw')
    // Only credentials of the requirement's service are offered.
    expect(select.findAllComponents(SelectItem).map((item) => item.text())).toEqual([
      'Each workspace adds its own',
      'Always use “Work Playwright”',
    ])

    await choose(wrapper, 'plugin-requirement-credential-0', 'cred-pw')
    await save(wrapper)
    expect(updated().credential_requirements).toEqual([
      { key: 'api_key', description: '', required: true, service_id: 'svc-1', credential_id: 'cred-pw' },
    ])
  })

  it('hands a further credential the plugin uses back to each workspace', async () => {
    credentialStoreMock.credentials = [credential({ id: 'cred-pw', name: 'Work Playwright' })]
    const wrapper = mountEditor(
      makePlugin({
        credential_requirements: [
          requirement({
            credential_id: 'cred-pw',
            credential_name: 'Work Playwright',
            credential_scope: 'personal',
          }),
        ],
      }),
    )
    await flushPromises()
    const select = wrapper
      .findAllComponents(Select)
      .find((entry) => entry.find('[data-testid="plugin-requirement-credential-0"]').exists())!
    expect(select.props('modelValue')).toBe('cred-pw')
    const eachWorkspace = select
      .findAllComponents(SelectItem)
      .find((item) => item.text() === 'Each workspace adds its own')!

    await choose(wrapper, 'plugin-requirement-credential-0', String(eachWorkspace.props('value')))
    await save(wrapper)
    expect(updated().credential_requirements).toEqual([
      { key: 'api_key', description: '', required: true, service_id: 'svc-1', credential_id: null },
    ])
  })

  it('signs in to a URL server with a per-user OAuth credential', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Notion')
    await wrapper.get('[data-testid="plugin-add-mcp"]').trigger('click')
    await mcp(wrapper).get('[id^="mcp-name-"]').setValue('Notion')
    await selectTab(wrapper, 'plugin-mcp-kind-url-0')
    // Sign-in needs an OAuth credential first.
    expect(wrapper.find('[data-testid="plugin-mcp-auth-0"]').exists()).toBe(false)

    await addRequirement(wrapper)
    await choose(wrapper, 'plugin-requirement-service-0', 'oauth-service')
    expect(editorForm(wrapper).requirements[0]).toMatchObject({
      serviceId: 'oauth-service',
      credentialType: 'mcp_oauth',
      reqKey: 'notion_oauth',
    })

    await choose(wrapper, 'plugin-mcp-auth-0', 'oauth')
    await choose(wrapper, 'plugin-mcp-oauth-requirement-0', 'notion_oauth')
    const url = wrapper.get('[data-testid="plugin-mcp-url-0"]')
    expect((url.element as HTMLInputElement).value).toBe('https://mcp.notion.com/mcp')
    expect(url.attributes('disabled')).toBeDefined()

    await save(wrapper)
    expect(created()).toMatchObject({
      mcp_servers: [
        {
          name: 'Notion',
          transport: 'streamable_http',
          url: 'https://mcp.notion.com/mcp',
          auth_type: 'oauth',
          oauth_requirement_key: 'notion_oauth',
        },
      ],
      credential_requirements: [
        { key: 'notion_oauth', required: true, service_id: 'oauth-service' },
      ],
    })
  })

  it('links the only OAuth server to a chosen OAuth service and forgets it when sign-in is off', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-add-mcp"]').trigger('click')
    await selectTab(wrapper, 'plugin-mcp-kind-url-0')
    await addRequirement(wrapper)
    await choose(wrapper, 'plugin-requirement-service-0', 'oauth-service')
    await choose(wrapper, 'plugin-mcp-auth-0', 'oauth')
    const form = editorForm(wrapper)
    expect(form.mcps[0]).toMatchObject({ authType: 'oauth', oauthRequirementKey: '' })

    await choose(wrapper, 'plugin-requirement-service-0', 'oauth-service')
    expect(form.mcps[0]).toMatchObject({
      oauthRequirementKey: 'notion_oauth',
      url: 'https://mcp.notion.com/mcp',
    })

    await choose(wrapper, 'plugin-mcp-auth-0', 'none')
    expect(form.mcps[0]).toMatchObject({ authType: 'none', oauthRequirementKey: '' })
  })

  it('keeps a typed requirement key and rejects an unknown service on save', async () => {
    const wrapper = mountEditor(null)
    await wrapper.get('[data-testid="plugin-name"]').setValue('Demo')
    expect(wrapper.find('[data-testid="plugin-requirement-0"]').exists()).toBe(false)
    await addRequirement(wrapper)
    const requirement = wrapper.get('[data-testid="plugin-requirement-0"]')
    await requirement.get('[aria-label="Key"]').setValue('token')

    await choose(wrapper, 'plugin-requirement-service-0', 'svc-1')
    expect(editorForm(wrapper).requirements[0]).toMatchObject({
      serviceId: 'svc-1',
      credentialType: 'env',
      reqKey: 'token',
    })

    await choose(wrapper, 'plugin-requirement-service-0', 'missing')
    expect(editorForm(wrapper).requirements[0]).toMatchObject({
      serviceId: 'missing',
      reqKey: 'token',
    })
    await save(wrapper)
    expect(pluginStoreMock.createPlugin).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="plugin-requirement-0"]').text()).toContain(
      'The credential service is not available here.',
    )
  })
})
