import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import WorkspacePluginCredentialSelection from './WorkspacePluginCredentialSelection.vue'
import { Select, SelectItem } from '@/components/ui/select'
import type { Credential } from '@/types'
import type { Plugin, PluginCredentialRequirement } from '@opencuria/chat-core/plugins'

const catalog = vi.hoisted(() => ({
  plugins: [] as Plugin[],
  error: null as string | null,
  loading: false,
  loadedOrgId: 'org-1',
  reload: vi.fn(async () => undefined),
  clear: vi.fn(),
}))
const visibleCredentials = vi.hoisted(() => ({
  credentials: [] as Credential[],
  error: null as string | null,
  loading: false,
  servicesLoaded: true,
  fetchCredentials: vi.fn(async () => undefined),
  fetchServices: vi.fn(async () => undefined),
}))
const auth = vi.hoisted(() => ({
  activeOrganizationId: 'org-1',
  isAdmin: false,
  user: { id: 7 } as { id: number } | null,
}))
vi.mock('@/stores/plugins', () => ({ usePluginStore: () => catalog }))
vi.mock('@/stores/credentials', () => ({ useCredentialStore: () => visibleCredentials }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: 'cred-1',
    name: 'Personal GitHub',
    scope: 'personal',
    service_id: 'svc-git',
    service_name: 'GitHub',
    service_slug: 'github',
    credential_type: 'env',
    env_var_name: 'TOKEN',
    target_path: '',
    has_public_key: false,
    created_by_id: 7,
    created_at: '',
    updated_at: '',
    oauth_connected: false,
    oauth_status: '',
    oauth_reconnect_required: false,
    oauth_expires_at: null,
    ...overrides,
  }
}
function oauthCredential(overrides: Partial<Credential> = {}): Credential {
  return credential({
    id: 'oauth-1',
    name: 'Personal Notion',
    service_id: 'svc-oauth',
    service_name: 'Notion',
    service_slug: 'notion',
    credential_type: 'mcp_oauth',
    env_var_name: '',
    oauth_connected: false,
    oauth_status: 'reconnect_required',
    oauth_reconnect_required: true,
    ...overrides,
  })
}
function requirement(
  overrides: Partial<PluginCredentialRequirement> = {},
): PluginCredentialRequirement {
  return {
    id: 'req-1',
    key: 'token',
    description: 'Required token',
    required: true,
    service_id: 'svc-git',
    service_name: 'GitHub',
    service_slug: 'github',
    credential_type: 'env',
    credential_id: null,
    credential_name: '',
    credential_scope: '',
    ...overrides,
  }
}
const oauthRequirement = (overrides: Partial<PluginCredentialRequirement> = {}) =>
  requirement({
    id: 'oauth-req',
    key: 'oauth',
    description: '',
    service_id: 'svc-oauth',
    service_name: 'Notion',
    service_slug: 'notion',
    credential_type: 'mcp_oauth',
    ...overrides,
  })
function plugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: 'plugin-1',
    name: 'Git helper',
    slug: 'git-helper',
    description: 'Tools',
    scope: 'builtin',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: true,
    org_enabled: true,
    available: true,
    editable: false,
    skills: [],
    mcp_servers: [],
    credential_requirements: [
      requirement(),
      requirement({
        id: 'req-2',
        key: 'ssh',
        description: 'Optional key',
        required: false,
        service_id: 'svc-ssh',
        service_name: 'SSH',
        service_slug: 'ssh',
        credential_type: 'ssh_key',
      }),
    ],
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}
function mountSelection(pluginIds: string[] = ['plugin-1'], credentialIds: string[] = []) {
  setActivePinia(createPinia())
  return mount(WorkspacePluginCredentialSelection, {
    props: { pluginIds, credentialIds },
  })
}

function requirementRow(wrapper: VueWrapper, key: string) {
  return wrapper.get(`[data-testid="workspace-plugin-requirement-${key}"]`)
}
/** The credential Select of a requirement, found by its service name. */
function requirementSelect(wrapper: VueWrapper, serviceName: string) {
  return wrapper
    .findAllComponents(Select)
    .find((select) => select.find(`[aria-label="Credential for ${serviceName}"]`).exists())
}
function lastValidity(wrapper: VueWrapper): boolean | undefined {
  return wrapper.emitted<[boolean]>('validityChange')?.slice(-1)[0]?.[0]
}
function isBlocked(wrapper: VueWrapper): boolean {
  return wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()
}
function pluginToggle(wrapper: VueWrapper, id = 'plugin-1') {
  return wrapper.get(`[data-testid="workspace-plugin-${id}"] button[aria-pressed]`)
}

beforeEach(() => {
  vi.clearAllMocks()
  catalog.plugins = [plugin()]
  catalog.error = null
  catalog.loading = false
  catalog.loadedOrgId = 'org-1'
  auth.activeOrganizationId = 'org-1'
  auth.isAdmin = false
  auth.user = { id: 7 }
  visibleCredentials.credentials = [credential()]
  visibleCredentials.error = null
})
afterEach(() => sessionStorage.clear())

describe('WorkspacePluginCredentialSelection requirements', () => {
  it('lists the credentials a selected plugin needs; optional ones do not block saving', async () => {
    const wrapper = mountSelection(['plugin-1'], ['cred-1'])
    await flushPromises()
    expect(pluginToggle(wrapper).attributes('aria-pressed')).toBe('true')

    const token = requirementRow(wrapper, 'token')
    expect(token.text()).toContain('GitHub')
    expect(token.text()).toContain('Personal GitHub')
    expect(requirementSelect(wrapper, 'GitHub')?.props('modelValue')).toBe('cred-1')
    expect(token.find('[aria-invalid="true"]').exists()).toBe(false)

    // The optional SSH key has no credential yet and does not block.
    const ssh = requirementRow(wrapper, 'ssh')
    expect(ssh.text()).toContain('SSH · optional')
    expect(ssh.find('[aria-label="Add SSH credential"]').text()).toContain('Add credential')
    expect(requirementSelect(wrapper, 'SSH')).toBeUndefined()

    expect(wrapper.text()).not.toContain('Needs a credential')
    expect(isBlocked(wrapper)).toBe(false)
    expect(lastValidity(wrapper)).toBe(true)
  })

  it('hides requirements of plugins that are not selected', async () => {
    const wrapper = mountSelection([], [])
    await flushPromises()
    expect(pluginToggle(wrapper).attributes('aria-pressed')).toBe('false')
    expect(wrapper.find('[data-testid="workspace-plugin-requirement-token"]').exists()).toBe(false)
    expect(isBlocked(wrapper)).toBe(false)
  })

  it('needs nothing from the workspace for a credential the plugin brings itself', async () => {
    catalog.plugins = [
      plugin({
        credential_requirements: [
          requirement({
            credential_id: 'plugin-cred',
            credential_name: 'Team token',
            credential_scope: 'organization',
          }),
        ],
      }),
    ]
    // A credential of the same service exists, but the plugin does not need it.
    const wrapper = mountSelection(['plugin-1'], [])
    await flushPromises()
    const token = requirementRow(wrapper, 'token')
    expect(token.text()).toContain('Uses “Team token”')
    expect(token.find('button').exists()).toBe(false)
    expect(requirementSelect(wrapper, 'GitHub')).toBeUndefined()
    expect(wrapper.text()).not.toContain('Needs a credential')
    expect(isBlocked(wrapper)).toBe(false)
    expect(lastValidity(wrapper)).toBe(true)
  })

  it('blocks a missing required credential and offers to choose or add one', async () => {
    const wrapper = mountSelection(['plugin-1'], [])
    await flushPromises()
    expect(isBlocked(wrapper)).toBe(true)
    expect(lastValidity(wrapper)).toBe(false)
    expect(wrapper.get('[data-testid="workspace-plugin-plugin-1"]').text()).toContain(
      'Needs a credential',
    )
    // A connected credential of the service exists: it can be chosen.
    const token = requirementRow(wrapper, 'token')
    expect(token.text()).toContain('Choose credential')
    expect(token.get('[aria-label="Credential for GitHub"]').attributes('aria-invalid')).toBe(
      'true',
    )

    await wrapper.setProps({ credentialIds: ['cred-1'] })
    expect(isBlocked(wrapper)).toBe(false)
    expect(lastValidity(wrapper)).toBe(true)
  })

  it('emits addCredential with the service when no credential of it exists', async () => {
    visibleCredentials.credentials = []
    const wrapper = mountSelection(['plugin-1'], [])
    await flushPromises()
    expect(isBlocked(wrapper)).toBe(true)
    const token = requirementRow(wrapper, 'token')
    expect(requirementSelect(wrapper, 'GitHub')).toBeUndefined()
    const add = token.get('[aria-label="Add GitHub credential"]')
    expect(add.text()).toBe('Add credential')
    await add.trigger('click')
    expect(wrapper.emitted('addCredential')).toEqual([['svc-git']])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('still offers adding another credential next to the existing choices', async () => {
    const wrapper = mountSelection(['plugin-1'], ['cred-1'])
    await flushPromises()
    const add = requirementRow(wrapper, 'token').get('[aria-label="Add GitHub credential"]')
    // Only the "+" icon: the Select already offers the existing ones.
    expect(add.text()).toBe('')
    await add.trigger('click')
    expect(wrapper.emitted('addCredential')).toEqual([['svc-git']])
  })

  it('choosing a credential replaces other selections of the same service only', async () => {
    visibleCredentials.credentials = [
      credential(),
      credential({ id: 'cred-2', name: 'Work GitHub' }),
      credential({ id: 'cred-other', name: 'Linear', service_id: 'svc-linear', service_name: 'Linear' }),
    ]
    const wrapper = mountSelection(['plugin-1'], ['cred-1', 'cred-other'])
    await flushPromises()
    const select = requirementSelect(wrapper, 'GitHub')!
    const offered = select.findAllComponents(SelectItem).map((item) => item.text())
    expect(offered).toEqual(['Personal GitHub', 'Work GitHub'])

    select.vm.$emit('update:modelValue', 'cred-2')
    await flushPromises()
    expect(wrapper.emitted('update:credentialIds')).toEqual([[['cred-other', 'cred-2']]])
  })

  it('offers None for an optional requirement and clears its service', async () => {
    visibleCredentials.credentials = [
      credential(),
      credential({
        id: 'ssh-1',
        name: 'Deploy key',
        service_id: 'svc-ssh',
        service_name: 'SSH',
        credential_type: 'ssh_key',
      }),
    ]
    const wrapper = mountSelection(['plugin-1'], ['cred-1', 'ssh-1'])
    await flushPromises()
    const ssh = requirementSelect(wrapper, 'SSH')!
    const none = ssh.findAllComponents(SelectItem).find((item) => item.text() === 'None')
    expect(none).toBeDefined()
    // Required requirements offer no "None".
    expect(
      requirementSelect(wrapper, 'GitHub')!
        .findAllComponents(SelectItem)
        .map((item) => item.text()),
    ).not.toContain('None')

    ssh.vm.$emit('update:modelValue', none!.props('value'))
    await flushPromises()
    expect(wrapper.emitted('update:credentialIds')).toEqual([[['cred-1']]])
  })

  it('lets two selected plugins share one required service credential', async () => {
    catalog.plugins = [
      plugin(),
      plugin({
        id: 'plugin-2',
        name: 'Git workflow',
        slug: 'git-workflow',
        credential_requirements: [requirement({ id: 'req-shared', key: 'git' })],
      }),
    ]
    const wrapper = mountSelection(['plugin-1', 'plugin-2'], ['cred-1'])
    await flushPromises()
    expect(isBlocked(wrapper)).toBe(false)
    expect(requirementRow(wrapper, 'token').text()).toContain('Personal GitHub')
    expect(requirementRow(wrapper, 'git').text()).toContain('Personal GitHub')
    expect(lastValidity(wrapper)).toBe(true)
  })
})

describe('WorkspacePluginCredentialSelection OAuth', () => {
  beforeEach(() => {
    catalog.plugins = [plugin({ credential_requirements: [oauthRequirement()] })]
  })

  it('accepts a selected connected OAuth credential', async () => {
    visibleCredentials.credentials = [
      oauthCredential({
        oauth_connected: true,
        oauth_status: 'connected',
        oauth_reconnect_required: false,
      }),
    ]
    const wrapper = mountSelection(['plugin-1'], ['oauth-1'])
    await flushPromises()
    expect(isBlocked(wrapper)).toBe(false)
    expect(requirementSelect(wrapper, 'Notion')?.props('modelValue')).toBe('oauth-1')
    expect(requirementRow(wrapper, 'oauth').text()).not.toContain('Reconnect')
  })

  it('does not count a disconnected OAuth credential and offers to reconnect it', async () => {
    visibleCredentials.credentials = [oauthCredential()]
    // Even when attached, a disconnected credential does not satisfy the plugin.
    const wrapper = mountSelection(['plugin-1'], ['oauth-1'])
    await flushPromises()
    expect(isBlocked(wrapper)).toBe(true)
    expect(lastValidity(wrapper)).toBe(false)
    const row = requirementRow(wrapper, 'oauth')
    // No connected credential to choose from.
    expect(requirementSelect(wrapper, 'Notion')).toBeUndefined()
    expect(row.find('[aria-label="Add Notion credential"]').exists()).toBe(true)

    const reconnect = row
      .findAll('button')
      .find((button) => button.text() === 'Reconnect Personal Notion')
    expect(reconnect).toBeDefined()
    await reconnect!.trigger('click')
    expect(wrapper.emitted('reconnectCredential')).toEqual([['oauth-1']])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('tells the user when only the owner can reconnect', async () => {
    visibleCredentials.credentials = [oauthCredential({ created_by_id: 99 })]
    const wrapper = mountSelection(['plugin-1'], [])
    await flushPromises()
    const row = requirementRow(wrapper, 'oauth')
    expect(row.text()).toContain('Personal Notion needs its owner to reconnect')
    expect(row.findAll('button').some((button) => button.text().startsWith('Reconnect'))).toBe(
      false,
    )
    expect(isBlocked(wrapper)).toBe(true)
  })
})

describe('WorkspacePluginCredentialSelection enabling plugins', () => {
  it('attaches the only connected credential of each service the plugin needs', async () => {
    visibleCredentials.credentials = [
      credential(),
      credential({
        id: 'ssh-1',
        name: 'Deploy key',
        service_id: 'svc-ssh',
        service_name: 'SSH',
        credential_type: 'ssh_key',
      }),
    ]
    const wrapper = mountSelection([], ['unrelated'])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:pluginIds')).toEqual([[['plugin-1']]])
    expect(wrapper.emitted('update:credentialIds')).toEqual([[['unrelated', 'cred-1', 'ssh-1']]])
  })

  it('leaves the choice to the user when a service has several credentials', async () => {
    visibleCredentials.credentials = [credential(), credential({ id: 'cred-2', name: 'Work GitHub' })]
    const wrapper = mountSelection([], [])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:pluginIds')).toEqual([[['plugin-1']]])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('keeps a credential of the service that is already attached', async () => {
    visibleCredentials.credentials = [credential(), credential({ id: 'cred-2', name: 'Work GitHub' })]
    const wrapper = mountSelection([], ['cred-2'])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('never attaches a disconnected OAuth credential', async () => {
    catalog.plugins = [plugin({ credential_requirements: [oauthRequirement()] })]
    visibleCredentials.credentials = [oauthCredential()]
    const wrapper = mountSelection([], [])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:pluginIds')).toEqual([[['plugin-1']]])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('attaches the connected OAuth credential next to a disconnected one', async () => {
    catalog.plugins = [plugin({ credential_requirements: [oauthRequirement()] })]
    visibleCredentials.credentials = [
      oauthCredential(),
      oauthCredential({ id: 'oauth-2', name: 'Work Notion', oauth_connected: true }),
    ]
    const wrapper = mountSelection([], [])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:credentialIds')).toEqual([[['oauth-2']]])
  })

  it('attaches nothing for a credential the plugin brings itself', async () => {
    catalog.plugins = [
      plugin({
        credential_requirements: [
          requirement({ credential_id: 'plugin-cred', credential_name: 'Team token' }),
        ],
      }),
    ]
    const wrapper = mountSelection([], [])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:pluginIds')).toEqual([[['plugin-1']]])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('turning a plugin off keeps the attached credentials', async () => {
    const wrapper = mountSelection(['plugin-1'], ['cred-1'])
    await flushPromises()
    await pluginToggle(wrapper).trigger('click')
    expect(wrapper.emitted('update:pluginIds')).toEqual([[[]]])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })
})

describe('WorkspacePluginCredentialSelection catalog and credentials', () => {
  it('offers personal and released plugins and hides ones the user cannot enable', async () => {
    catalog.plugins = [
      plugin({ credential_requirements: [] }),
      plugin({
        id: 'mine',
        name: 'My tools',
        scope: 'personal',
        is_global: false,
        org_enabled: false,
        editable: true,
        credential_requirements: [],
      }),
      plugin({
        id: 'unreleased',
        name: 'Unreleased tools',
        scope: 'organization',
        organization_id: 'org-1',
        is_global: false,
        org_enabled: false,
        available: false,
      }),
    ]
    const wrapper = mountSelection([], [])
    await flushPromises()
    expect(wrapper.text()).toContain('Git helper')
    expect(wrapper.text()).toContain('My tools')
    expect(wrapper.text()).not.toContain('Unreleased tools')
  })

  it('preserves unavailable persisted plugin ids and blocks until the user removes them', async () => {
    catalog.plugins = []
    const wrapper = mountSelection(['removed-plugin'], ['cred-1'])
    await flushPromises()
    expect(wrapper.text()).toContain('Unavailable plugin')
    expect(wrapper.text()).toContain('remove to save')
    expect(isBlocked(wrapper)).toBe(false)
    expect(lastValidity(wrapper)).toBe(false)
    const toggle = wrapper.find('button[aria-pressed="true"]')
    await toggle.trigger('click')
    expect(wrapper.emitted('update:pluginIds')?.slice(-1)[0]).toEqual([[]])
  })

  it('toggles credentials in the list, one per service', async () => {
    visibleCredentials.credentials = [credential(), credential({ id: 'cred-2', name: 'Work GitHub' })]
    const wrapper = mountSelection([], ['cred-1'])
    await flushPromises()
    const listed = wrapper
      .findAll('button[aria-pressed]')
      .filter((button) => button.text().includes('GitHub'))
    expect(listed.map((button) => button.attributes('aria-pressed'))).toEqual(['true', 'false'])

    await listed[1]!.trigger('click')
    await listed[0]!.trigger('click')
    expect(wrapper.emitted('update:credentialIds')).toEqual([[['cred-2']], [[]]])
  })

  it('asks to reconnect a disconnected OAuth credential before attaching it from the list', async () => {
    visibleCredentials.credentials = [oauthCredential()]
    const wrapper = mountSelection([], [])
    await flushPromises()
    const listed = wrapper
      .findAll('button[aria-pressed]')
      .find((button) => button.text().includes('Personal Notion'))!
    expect(listed.text()).toContain('Reconnect')
    await listed.trigger('click')
    expect(wrapper.emitted('reconnectCredential')).toEqual([['oauth-1']])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })
})
