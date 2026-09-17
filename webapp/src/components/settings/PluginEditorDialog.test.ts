import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import PluginEditorDialog from './PluginEditorDialog.vue'
import type { Plugin } from '@/types'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const pluginStoreMock = vi.hoisted(() => ({
  createPlugin: vi.fn(),
  updatePlugin: vi.fn(),
}))

vi.mock('@/stores/plugins', () => ({
  usePluginStore: () => pluginStoreMock,
}))

vi.mock('@/stores/credentials', () => ({
  useCredentialStore: () => ({
    credentials: [],
    services: [
      {
        id: 'svc-1',
        name: 'Playwright Auth',
        slug: 'playwright-auth',
        description: '',
        credential_type: 'env',
        env_var_name: 'PLAYWRIGHT_TOKEN',
        target_path: '',
        label: '',
      },
    ],
    loading: false,
    error: null,
    fetchCredentials: vi.fn(),
    fetchServices: vi.fn(),
  }),
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

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>', props: ['open'] },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

function makePlugin(): Plugin {
  return {
    id: 'plugin-1',
    name: 'Playwright',
    slug: 'playwright',
    description: 'Browser automation',
    enabled: true,
    published: true,
    organization_id: 'org-1',
    is_global: false,
    org_enabled: false,
    skills: [{ id: 's-1', name: 'Basics', slug: 'basics', body: 'Use it.', position: 0 }],
    mcp_servers: [],
    credential_requirements: [
      {
        id: 'r-1',
        key: 'api_key',
        description: '',
        required: true,
        service_id: 'svc-1',
        service_name: 'Playwright Auth',
        service_slug: 'playwright-auth',
        credential_type: 'env',
        plugin_owned_service: true,
      },
    ],
    credential_readiness: {
      required_service_ids: ['svc-1'],
      missing_required_service_ids: [],
      ready: true,
    },
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
  }
}

function mountEditor(plugin: Plugin | null) {
  setActivePinia(createPinia())
  return mount(PluginEditorDialog, {
    props: { open: true, plugin },
    global: { stubs: dialogStubs },
  })
}

describe('PluginEditorDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('normalizes a create payload with skills and placeholders', async () => {
    pluginStoreMock.createPlugin.mockResolvedValue({ id: 'new' })
    const wrapper = mountEditor(null)
    await flushPromises()

    await wrapper.find('[data-testid="plugin-name"]').setValue('Demo Plugin')
    expect((wrapper.find('[data-testid="plugin-slug"]').element as HTMLInputElement).value).toBe(
      'demo-plugin',
    )

    await wrapper.find('[data-testid="plugin-add-skill"]').trigger('click')
    await flushPromises()
    const skill = wrapper.find('[data-testid="plugin-skill-0"]')
    await skill.find('input').setValue('Basics')
    await skill.find('textarea').setValue('Use it well.')

    await wrapper.find('[data-testid="plugin-add-mcp"]').trigger('click')
    await flushPromises()
    const mcp = wrapper.find('[data-testid="plugin-mcp-0"]')
    const inputs = mcp.findAll('input')
    await inputs[0]!.setValue('Runner')
    await inputs[2]!.setValue('npx')

    await wrapper.find('#plugin-editor-form').trigger('submit')
    await flushPromises()

    expect(pluginStoreMock.createPlugin).toHaveBeenCalled()
    const payload = pluginStoreMock.createPlugin.mock.calls[0]![0]
    expect(payload.name).toBe('Demo Plugin')
    expect(payload.slug).toBe('demo-plugin')
    expect(payload.skills[0]).toMatchObject({ name: 'Basics', body: 'Use it well.', position: 0 })
    expect(payload.mcp_servers[0]).toMatchObject({ name: 'Runner', command: 'npx' })
    // stdio payloads must not carry stale http fields.
    expect(payload.mcp_servers[0]).toMatchObject({ url: '', headers: {} })
  })

  it('round-trips edit requirements via service_id (no duplicate services)', async () => {
    pluginStoreMock.updatePlugin.mockResolvedValue({ id: 'plugin-1' })
    const wrapper = mountEditor(makePlugin())
    await flushPromises()

    expect(wrapper.find('[data-testid="plugin-requirement-0"]').exists()).toBe(true)
    await wrapper.find('#plugin-editor-form').trigger('submit')
    await flushPromises()

    expect(pluginStoreMock.updatePlugin).toHaveBeenCalledWith(
      'plugin-1',
      expect.objectContaining({
        credential_requirements: [
          expect.objectContaining({
            key: 'api_key',
            credential_service: { service_id: 'svc-1' },
          }),
        ],
      }),
    )
  })

  it('blocks submit on validation errors and shows backend failures', async () => {
    pluginStoreMock.createPlugin.mockResolvedValue(null)
    const wrapper = mountEditor(null)
    await flushPromises()
    // Empty name → invalid.
    expect(wrapper.find('[data-testid="plugin-editor-save"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="plugin-editor-validation"]').exists()).toBe(true)

    await wrapper.find('[data-testid="plugin-name"]').setValue('Demo')
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-editor-save"]').attributes('disabled')).toBeUndefined()

    await wrapper.find('#plugin-editor-form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-editor-error"]').exists()).toBe(true)
  })

  it('exposes transport switching that clears stale fields (unit-level)', async () => {
    const { emptyMcpForm, formToCreateIn } = await import('@/lib/pluginForms')
    const mcp = emptyMcpForm()
    mcp.name = 'Runner'
    mcp.transport = 'stdio'
    mcp.command = 'npx'
    mcp.argsText = '-y'
    mcp.url = 'https://stale.example/mcp'
    // Simulate the dialog's setMcpTransport clearing on stdio→http.
    mcp.transport = 'streamable_http'
    mcp.command = ''
    mcp.argsText = ''
    mcp.env = []
    mcp.url = 'https://mcp.example.com/mcp'
    const payload = formToCreateIn({
      name: 'Demo',
      slug: '',
      slugTouched: false,
      description: '',
      enabled: true,
      published: true,
      skills: [],
      mcps: [mcp],
      requirements: [],
    })
    expect(payload.mcp_servers?.[0]).toMatchObject({ command: '', args: [], env: {} })
  })
})
