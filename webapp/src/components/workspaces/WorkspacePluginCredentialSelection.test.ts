import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import WorkspacePluginCredentialSelection from './WorkspacePluginCredentialSelection.vue'
import type { Credential, Plugin } from '@/types'

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
const auth = vi.hoisted(() => ({ activeOrganizationId: 'org-1' }))
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
function plugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: 'plugin-1',
    name: 'Git helper',
    slug: 'git-helper',
    description: 'Tools',
    enabled: true,
    published: true,
    organization_id: null,
    is_global: true,
    org_enabled: true,
    skills: [],
    mcp_servers: [],
    credential_requirements: [
      {
        id: 'req-1',
        key: 'token',
        description: 'Required token',
        required: true,
        service_id: 'svc-git',
        service_name: 'GitHub',
        service_slug: 'github',
        credential_type: 'env',
      },
      {
        id: 'req-2',
        key: 'ssh',
        description: 'Optional key',
        required: false,
        service_id: 'svc-ssh',
        service_name: 'SSH',
        service_slug: 'ssh',
        credential_type: 'ssh_key',
      },
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
beforeEach(() => {
  vi.clearAllMocks()
  catalog.plugins = [plugin()]
  catalog.error = null
  catalog.loading = false
  catalog.loadedOrgId = 'org-1'
  auth.activeOrganizationId = 'org-1'
  visibleCredentials.credentials = [credential()]
  visibleCredentials.error = null
})
afterEach(() => sessionStorage.clear())

describe('WorkspacePluginCredentialSelection', () => {
  it('shows requirement labels and optional requirements do not block save', async () => {
    const wrapper = mountSelection(['plugin-1'], ['cred-1'])
    await flushPromises()
    expect(wrapper.text()).toContain('Required')
    expect(wrapper.text()).toContain('Optional')
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(false)
    expect(wrapper.find('button').attributes('aria-pressed')).toBe('true')
  })

  it('lets two selected plugins share one required service credential', async () => {
    catalog.plugins = [
      plugin(),
      plugin({
        id: 'plugin-2', name: 'Git workflow', slug: 'git-workflow',
        credential_requirements: [
          { id: 'req-shared', key: 'token', description: '', required: true, service_id: 'svc-git', service_name: 'GitHub', service_slug: 'github', credential_type: 'env' },
        ],
      }),
    ]
    const wrapper = mountSelection(['plugin-1', 'plugin-2'], ['cred-1'])
    await flushPromises()
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Selected:')
    expect(wrapper.text()).toContain('Personal GitHub')
  })

  it('blocks missing required credentials, emits an add service link, and does not attach credentials implicitly', async () => {
    const wrapper = mountSelection(['plugin-1'], [])
    await flushPromises()
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(true)
    const add = wrapper
      .findAll('button')
      .find((button) => button.text().includes('Add GitHub credential'))!
    await add.trigger('click')
    expect(wrapper.emitted('addCredential')).toEqual([['svc-git']])
    expect(wrapper.emitted('update:credentialIds')).toBeUndefined()
  })

  it('accepts one explicitly selected connected OAuth credential from the real API list', async () => {
    catalog.plugins = [
      plugin({
        credential_requirements: [
          { id: 'oauth-req', key: 'oauth', description: '', required: true, service_id: 'svc-oauth', service_name: 'Notion', service_slug: 'notion', credential_type: 'mcp_oauth' },
        ],
      }),
    ]
    visibleCredentials.credentials = [
      credential({ id: 'oauth-1', service_id: 'svc-oauth', service_name: 'Notion', credential_type: 'mcp_oauth', oauth_connected: true }),
    ]
    const wrapper = mountSelection(['plugin-1'], ['oauth-1'])
    await flushPromises()
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Selected:')
    expect(wrapper.text()).toContain('Notion')
  })

  it('uses real OAuth credential connection metadata and requires an explicit reconnect', async () => {
    catalog.plugins = [
      plugin({
        credential_requirements: [
          {
            id: 'oauth-req',
            key: 'oauth',
            description: '',
            required: true,
            service_id: 'svc-oauth',
            service_name: 'Notion',
            service_slug: 'notion',
            credential_type: 'mcp_oauth',
          },
        ],
      }),
    ]
    visibleCredentials.credentials = [
      credential({
        id: 'oauth-1',
        service_id: 'svc-oauth',
        service_name: 'Notion',
        credential_type: 'mcp_oauth',
        oauth_connected: false,
      }),
    ]
    const wrapper = mountSelection(['plugin-1'], ['oauth-1'])
    await flushPromises()
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Reconnect required')
    expect(wrapper.text()).not.toContain('Connected credential selected.')
  })

  it('preserves unavailable persisted plugin ids and blocks until the user removes them', async () => {
    catalog.plugins = []
    const wrapper = mountSelection(['removed-plugin'], ['cred-1'])
    await flushPromises()
    expect(wrapper.text()).toContain('Unavailable plugin')
    expect(wrapper.text()).toContain('remove to save')
    expect(wrapper.find('[data-testid="workspace-plugins-blocked"]').exists()).toBe(false)
    const toggle = wrapper.find('button[aria-pressed="true"]')
    await toggle.trigger('click')
    expect(wrapper.emitted('update:pluginIds')?.slice(-1)[0]).toEqual([[]])
  })
})
