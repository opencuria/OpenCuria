import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import PluginEditorDialog from './PluginEditorDialog.vue'
import { Select } from '@/components/ui/select'
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
    servicesLoaded: true,
    servicesError: null,
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
        organization_id: 'org-1',
        oauth_server_url: '',
        is_active: true,
      },
      {
        id: 'oauth-service',
        name: 'Notion OAuth',
        slug: 'notion-oauth',
        description: '',
        credential_type: 'mcp_oauth',
        env_var_name: '',
        target_path: '',
        label: '',
        organization_id: 'org-1',
        oauth_server_url: 'https://mcp.notion.com/mcp',
        is_active: true,
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
      },
    ],
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

  it('retains a failed edit for another Save attempt', async () => {
    pluginStoreMock.updatePlugin.mockResolvedValueOnce(null).mockResolvedValue({ id: 'plugin-1' })
    const wrapper = mountEditor(makePlugin())
    await wrapper.get('[data-testid="plugin-name"]').setValue('Updated plugin')
    await wrapper.get('#plugin-editor-form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="plugin-editor-error"]').text()).toContain('Failed to update')
    expect(wrapper.emitted('update:open')).toBeUndefined()
    await wrapper.get('#plugin-editor-form').trigger('submit')
    await flushPromises()
    expect(pluginStoreMock.updatePlugin).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('update:open')).toEqual([[false]])
  })

  it('normalizes a create payload with skills and placeholders', async () => {
    pluginStoreMock.createPlugin.mockResolvedValue({ id: 'new' })
    const wrapper = mountEditor(null)
    await flushPromises()

    await wrapper.find('[data-testid="plugin-name"]').setValue('Demo Plugin')
    // No slug input is shown anymore; the backend derives the slug.
    expect(wrapper.find('[data-testid="plugin-slug"]').exists()).toBe(false)

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
    await inputs[1]!.setValue('npx')

    await wrapper.find('#plugin-editor-form').trigger('submit')
    await flushPromises()

    expect(pluginStoreMock.createPlugin).toHaveBeenCalled()
    const payload = pluginStoreMock.createPlugin.mock.calls[0]![0]
    expect(payload.name).toBe('Demo Plugin')
    // The backend derives slugs from names; payloads carry empty slugs.
    expect(payload.slug).toBe('')
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
            description: '',
            required: true,
            service_id: 'svc-1',
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
    expect(
      wrapper.find('[data-testid="plugin-editor-save"]').attributes('disabled'),
    ).toBeUndefined()

    await wrapper.find('#plugin-editor-form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="plugin-editor-error"]').exists()).toBe(true)
  })

  it('configures HTTP OAuth auth and round-trips its managed requirement', async () => {
    pluginStoreMock.createPlugin.mockResolvedValue({ id: 'new' })
    const wrapper = mountEditor(null)
    await flushPromises()
    await wrapper.find('[data-testid="plugin-name"]').setValue('Notion')
    await wrapper.find('[data-testid="plugin-add-mcp"]').trigger('click')
    const mcp = wrapper.find('[data-testid="plugin-mcp-0"]')
    await mcp.find('input').setValue('Notion')
    const { emptyPluginForm, emptyMcpForm, formToCreateIn, validatePluginForm } =
      await import('@/lib/pluginForms')
    const form = emptyPluginForm()
    form.name = 'Notion'
    form.requirements.push({
      uid: 'r',
      reqKey: 'notion_oauth',
      description: '',
      required: true,
      serviceId: 'oauth-service',
      credentialType: 'mcp_oauth',
    })
    const server = emptyMcpForm()
    server.name = 'Notion'
    server.transport = 'streamable_http'
    server.url = 'https://mcp.notion.com/mcp'
    server.authType = 'oauth'
    server.oauthRequirementKey = 'notion_oauth'
    form.mcps.push(server)
    expect(validatePluginForm(form)).toEqual([])
    expect(formToCreateIn(form).mcp_servers?.[0]).toMatchObject({
      auth_type: 'oauth',
      oauth_requirement_key: 'notion_oauth',
    })
    expect(formToCreateIn(form).credential_requirements?.[0]?.service_id).toBe('oauth-service')
  })

  it('rejects mismatched OAuth endpoint and requires an existing OAuth service', async () => {
    const { emptyPluginForm, emptyMcpForm, validatePluginForm } = await import('@/lib/pluginForms')
    const form = emptyPluginForm()
    form.name = 'Notion'
    form.requirements.push({
      uid: 'req',
      reqKey: 'notion',
      description: '',
      required: true,
      serviceId: 'oauth-service',
      credentialType: 'mcp_oauth',
    })
    const server = emptyMcpForm()
    server.name = 'Notion MCP'
    server.transport = 'streamable_http'
    server.url = 'https://different.notion.test/mcp'
    server.authType = 'oauth'
    server.oauthRequirementKey = 'notion'
    form.mcps.push(server)
    form.availableServices = [
      {
        id: 'oauth-service',
        name: 'Notion OAuth',
        slug: 'notion-oauth',
        description: '',
        credential_type: 'mcp_oauth',
        env_var_name: '',
        target_path: '',
        label: '',
        organization_id: 'org-1',
        oauth_server_url: 'https://mcp.notion.com/mcp',
        is_active: true,
      },
    ]
    expect(validatePluginForm(form).some((error) => error.includes('exactly match'))).toBe(true)
  })

  it('exposes transport switching that clears stale fields (unit-level)', async () => {
    const { emptyMcpForm, formToCreateIn, setMcpTransport } = await import('@/lib/pluginForms')
    const mcp = emptyMcpForm()
    mcp.name = 'Runner'
    mcp.transport = 'stdio'
    mcp.command = 'npx'
    mcp.argsText = '-y'
    mcp.env = [{ uid: 'e', key: 'TOKEN', value: 'x' }]
    const form = setMcpTransport(
      {
        name: 'Demo',
        description: '',
        enabled: true,
        published: true,
        skills: [],
        mcps: [mcp],
        requirements: [],
      },
      mcp.uid,
      'streamable_http',
    )
    form.mcps[0]!.url = 'https://mcp.example.com/mcp'
    const payload = formToCreateIn(form)
    expect(payload.mcp_servers?.[0]).toMatchObject({ command: '', args: [], env: {} })
  })
})

describe('PluginEditorDialog form rules', () => {
  type EditorForm = ReturnType<(typeof import('@/lib/pluginForms'))['emptyPluginForm']>

  function editorForm(wrapper: ReturnType<typeof mountEditor>): EditorForm {
    return (wrapper.vm as unknown as { form: EditorForm }).form
  }

  async function choose(wrapper: ReturnType<typeof mountEditor>, testId: string, value: string) {
    const select = wrapper
      .findAllComponents(Select)
      .find((candidate) => candidate.find(`[data-testid="${testId}"]`).exists())
    expect(select, testId).toBeDefined()
    select!.vm.$emit('update:modelValue', value)
    await flushPromises()
  }

  beforeEach(() => {
    localStorage.setItem('kern_active_org_id', 'org-1')
  })

  afterEach(() => {
    localStorage.removeItem('kern_active_org_id')
  })

  it('clears what a new transport forbids and forgets OAuth when it is turned off', async () => {
    const wrapper = mountEditor(null)
    await flushPromises()
    await wrapper.find('[data-testid="plugin-add-mcp"]').trigger('click')
    const form = editorForm(wrapper)
    Object.assign(form.mcps[0]!, {
      command: 'npx',
      argsText: '-y',
      desktop: 'first_tool',
      env: [{ uid: 'e', key: 'TOKEN', value: 'x' }],
    })

    await choose(wrapper, 'plugin-mcp-transport-0', 'streamable_http')
    expect(form.mcps[0]).toMatchObject({
      transport: 'streamable_http',
      command: '',
      argsText: '',
      desktop: 'none',
      env: [],
    })

    await choose(wrapper, 'plugin-mcp-auth-0', 'oauth')
    form.mcps[0]!.oauthRequirementKey = 'notion'
    form.mcps[0]!.url = 'https://mcp.example.com/mcp'
    form.mcps[0]!.headers = [{ uid: 'h', key: 'X-Team', value: 'a' }]
    await choose(wrapper, 'plugin-mcp-auth-0', 'none')
    expect(form.mcps[0]).toMatchObject({ authType: 'none', oauthRequirementKey: '' })

    await choose(wrapper, 'plugin-mcp-auth-0', 'oauth')
    form.mcps[0]!.oauthRequirementKey = 'notion'
    await choose(wrapper, 'plugin-mcp-transport-0', 'stdio')
    expect(form.mcps[0]).toMatchObject({
      transport: 'stdio',
      url: '',
      headers: [],
      authType: 'none',
      oauthRequirementKey: '',
    })
  })

  it('links an OAuth requirement service to the only OAuth server and its endpoint', async () => {
    const wrapper = mountEditor(null)
    await flushPromises()
    await wrapper.find('[data-testid="plugin-add-mcp"]').trigger('click')
    await wrapper.find('[data-testid="plugin-add-requirement"]').trigger('click')
    const form = editorForm(wrapper)
    await choose(wrapper, 'plugin-mcp-transport-0', 'streamable_http')
    await choose(wrapper, 'plugin-mcp-auth-0', 'oauth')

    await choose(wrapper, 'plugin-requirement-service-0', 'oauth-service')
    expect(form.requirements[0]).toMatchObject({
      serviceId: 'oauth-service',
      credentialType: 'mcp_oauth',
      reqKey: 'notion_oauth',
    })
    expect(form.mcps[0]).toMatchObject({
      oauthRequirementKey: 'notion_oauth',
      url: 'https://mcp.notion.com/mcp',
    })

    form.mcps[0]!.url = 'https://stale.example/mcp'
    await choose(wrapper, 'plugin-mcp-oauth-requirement-0', 'notion_oauth')
    expect(form.mcps[0]!.url).toBe('https://mcp.notion.com/mcp')
  })

  it('keeps a typed requirement key and only records an unknown service id', async () => {
    const wrapper = mountEditor(null)
    await flushPromises()
    await wrapper.find('[data-testid="plugin-add-requirement"]').trigger('click')
    const form = editorForm(wrapper)
    form.requirements[0]!.reqKey = 'token'

    await choose(wrapper, 'plugin-requirement-service-0', 'svc-1')
    expect(form.requirements[0]).toMatchObject({
      serviceId: 'svc-1',
      credentialType: 'env',
      reqKey: 'token',
    })

    await choose(wrapper, 'plugin-requirement-service-0', 'missing')
    expect(form.requirements[0]).toMatchObject({
      serviceId: 'missing',
      credentialType: 'env',
      reqKey: 'token',
    })
  })
})

it('offers one managed desktop selector and surfaces explicit env conflicts', async () => {
  const wrapper = mountEditor(null)
  await flushPromises()
  const form = (
    wrapper.vm as unknown as {
      form: ReturnType<(typeof import('@/lib/pluginForms'))['emptyPluginForm']>
    }
  ).form
  const { emptyMcpForm } = await import('@/lib/pluginForms')
  const mcp = emptyMcpForm()
  mcp.name = 'Browser'
  mcp.command = 'npx'
  mcp.desktop = 'first_tool'
  mcp.env = [{ uid: 'display', key: 'DISPLAY', value: ':9' }]
  form.name = 'Browser'
  form.mcps.push(mcp)
  await flushPromises()
  expect(wrapper.find('[data-testid="plugin-mcp-desktop-0"]').exists()).toBe(true)
  expect(wrapper.find('[data-testid="plugin-editor-validation"]').text()).toContain(
    'remove those env entries',
  )
  expect(wrapper.find('[data-testid="plugin-editor-save"]').attributes('disabled')).toBeDefined()
})
