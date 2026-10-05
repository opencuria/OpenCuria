import { describe, expect, it } from 'vitest'

import {
  emptyPluginForm,
  formToCreateIn,
  formToUpdateIn,
  parseArgsText,
  pluginToForm,
  rowsToDict,
  slugify,
  validatePluginForm,
} from './pluginForms'
import type { Plugin } from '@/types'

function makePlugin(): Plugin {
  return {
    id: 'plugin-1',
    name: 'Playwright',
    slug: 'playwright',
    description: 'Browser automation',
    enabled: true,
    published: false,
    organization_id: 'org-1',
    is_global: false,
    org_enabled: false,
    skills: [{ id: 's-1', name: 'Basics', slug: 'basics', body: 'Use it.', position: 0 }],
    mcp_servers: [
      {
        id: 'm-1',
        name: 'Runner',
        slug: 'runner',
        transport: 'stdio',
        command: 'npx',
        args: ['-y', '@playwright/mcp@latest'],
        cwd: '/workspace',
        env: { TOKEN: '{{credential.API_KEY}}' },
        url: '',
        headers: {},
        startup_timeout_seconds: 30,
        request_timeout_seconds: 60,
        auth_type: 'none',
        oauth_requirement_key: '',
      },
    ],
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

describe('pluginForms', () => {
  it('slugifies names', () => {
    expect(slugify('Playwright Helper!')).toBe('playwright-helper')
    expect(slugify('  __x__ ')).toBe('x')
  })

  it('parses args text and key/value rows', () => {
    expect(parseArgsText('-y\n\n @playwright/mcp@latest \n')).toEqual([
      '-y',
      '@playwright/mcp@latest',
    ])
    expect(
      rowsToDict([
        { uid: 'a', key: 'K', value: 'v' },
        { uid: 'b', key: ' ', value: 'x' },
      ]),
    ).toEqual({
      K: 'v',
    })
  })

  it('round-trips a plugin to a form via service_id references', () => {
    const form = pluginToForm(makePlugin())
    expect(form.skills).toHaveLength(1)
    expect(form.mcps[0]?.argsText).toBe('-y\n@playwright/mcp@latest')
    expect(form.mcps[0]?.env[0]).toMatchObject({ key: 'TOKEN', value: '{{credential.API_KEY}}' })
    expect(form.requirements[0]?.serviceId).toBe('svc-1')
    expect(form.requirements[0]?.serviceId).toBe('svc-1')

    const update = formToUpdateIn(form)
    expect(update.credential_requirements?.[0]).toMatchObject({
      key: 'api_key',
      description: '',
      required: true,
      service_id: 'svc-1',
    })
    expect(update.credential_requirements?.[0]).not.toHaveProperty('credential_service')
    expect(update.skills?.[0]?.position).toBe(0)
    expect(update.mcp_servers?.[0]?.command).toBe('npx')
  })

  it('round-trips OAuth MCP auth and validates its unique required service binding', () => {
    const form = emptyPluginForm()
    form.name = 'Notion'
    form.requirements.push({
      uid: 'req-oauth',
      reqKey: 'notion_oauth',
      description: 'Authorize Notion',
      required: true,
      serviceId: 'oauth-service',
      credentialType: 'mcp_oauth',
    })
    form.mcps.push({
      uid: 'mcp-oauth',
      name: 'Notion',
      transport: 'streamable_http',
      desktop: 'none',
      command: '',
      argsText: '',
      cwd: '/workspace',
      env: [],
      headers: [],
      url: 'https://mcp.example.com/mcp',
      startupTimeout: 30,
      requestTimeout: 60,
      authType: 'oauth',
      oauthRequirementKey: 'notion_oauth',
    })
    expect(validatePluginForm(form)).toEqual([])
    expect(formToCreateIn(form).mcp_servers?.[0]).toMatchObject({
      auth_type: 'oauth',
      oauth_requirement_key: 'notion_oauth',
    })
    expect(formToCreateIn(form).credential_requirements?.[0]).toMatchObject({
      key: 'notion_oauth',
      description: 'Authorize Notion',
      required: true,
      service_id: 'oauth-service',
    })

    const existing = makePlugin()
    existing.mcp_servers = [
      {
        id: 'server-oauth',
        name: 'Notion',
        slug: 'notion',
        transport: 'streamable_http',
        command: '',
        args: [],
        cwd: '/workspace',
        env: {},
        url: 'https://mcp.example.com/mcp',
        headers: {},
        startup_timeout_seconds: 30,
        request_timeout_seconds: 60,
        auth_type: 'oauth',
        oauth_requirement_key: 'notion_oauth',
      },
    ]
    existing.credential_requirements = [
      {
        id: 'req-oauth',
        key: 'notion_oauth',
        description: '',
        required: true,
        service_id: 'oauth-service',
        service_name: 'Notion OAuth',
        service_slug: 'notion-oauth',
        credential_type: 'mcp_oauth',
      },
    ]
    const update = formToUpdateIn(pluginToForm(existing))
    expect(update.mcp_servers?.[0]).toMatchObject({
      auth_type: 'oauth',
      oauth_requirement_key: 'notion_oauth',
    })
    expect(update.credential_requirements?.[0]?.service_id).toBe('oauth-service')

    form.mcps[0]!.oauthRequirementKey = 'other'
    expect(
      validatePluginForm(form).some((error) =>
        error.includes('required MCP OAuth credential service'),
      ),
    ).toBe(true)
    form.mcps[0]!.oauthRequirementKey = 'notion_oauth'
    const service = {
      id: 'oauth-service',
      name: 'Notion OAuth',
      slug: 'notion-oauth',
      description: '',
      credential_type: 'mcp_oauth',
      env_var_name: '',
      target_path: '',
      label: '',
      oauth_server_url: 'https://mcp.example.com/mcp',
      organization_id: 'org-1',
      is_active: true,
    }
    form.availableServices = [service]
    form.mcps[0]!.url = 'https://different.example.com/mcp'
    expect(validatePluginForm(form).some((error) => error.includes('exactly match'))).toBe(true)
    form.mcps[0]!.url = service.oauth_server_url
    form.mcps.push({ ...form.mcps[0]!, uid: 'mcp-duplicate', name: 'Duplicate' })
    expect(validatePluginForm(form).some((error) => error.includes('exactly one'))).toBe(true)
  })

  it('validates required fields, transports, and requirement sources', () => {
    const form = emptyPluginForm()
    expect(validatePluginForm(form)).toContain('Plugin name is required.')

    form.name = 'Demo'
    form.mcps.push({
      uid: 'm1',
      name: 'Runner',
      transport: 'stdio',
      desktop: 'none',
      command: '',
      argsText: '',
      cwd: '/workspace',
      env: [],
      headers: [],
      url: 'https://x.example',
      startupTimeout: 0,
      requestTimeout: 700,
      authType: 'none',
      oauthRequirementKey: '',
    })
    form.requirements.push({
      uid: 'r1',
      reqKey: 'api_key',
      description: '',
      required: true,
      serviceId: '',
      credentialType: 'env',
    })
    const errors = validatePluginForm(form)
    expect(errors.some((e) => e.includes('command is required'))).toBe(true)
    expect(errors.some((e) => e.includes('URL must be empty'))).toBe(true)
    expect(errors.some((e) => e.includes('startup timeout'))).toBe(true)
    expect(errors.some((e) => e.includes('existing credential service'))).toBe(true)
  })

  it('clears unused transport fields so stale values never ship', () => {
    const form = emptyPluginForm()
    form.name = 'Demo'
    form.requirements.push({
      uid: 'r1',
      reqKey: 'api_key',
      description: '',
      required: true,
      serviceId: 'svc-1',
      credentialType: 'env',
    })
    form.mcps.push({
      uid: 'm1',
      name: 'Runner',
      transport: 'stdio',
      desktop: 'none',
      command: 'npx',
      argsText: '-y',
      cwd: '/workspace',
      env: [{ uid: 'e1', key: 'TOKEN', value: '{{credential.api_key}}' }],
      // Stale http fields must be dropped for stdio.
      headers: [{ uid: 'h1', key: 'Authorization', value: 'Bearer x' }],
      url: 'https://stale.example/mcp',
      startupTimeout: 30,
      requestTimeout: 60,
      authType: 'none',
      oauthRequirementKey: '',
    })
    const stdio = formToCreateIn(form).mcp_servers?.[0]
    expect(stdio?.url).toBe('')
    expect(stdio?.headers).toEqual({})
    expect(stdio?.env).toEqual({ TOKEN: '{{credential.api_key}}' })

    form.mcps[0]!.transport = 'streamable_http'
    form.mcps[0]!.url = 'https://mcp.example.com/mcp'
    form.mcps[0]!.command = 'npx'
    form.mcps[0]!.argsText = '-y'
    form.mcps[0]!.env = [{ uid: 'e1', key: 'TOKEN', value: 'x' }]
    form.mcps[0]!.headers = [
      { uid: 'h1', key: 'Authorization', value: 'Bearer {{credential.api_key}}' },
    ]
    const http = formToCreateIn(form).mcp_servers?.[0]
    expect(http?.command).toBe('')
    expect(http?.args).toEqual([])
    expect(http?.env).toEqual({})
    expect(http?.headers).toEqual({ Authorization: 'Bearer {{credential.api_key}}' })
  })

  it('rejects unknown placeholders, bad templates, duplicates, and unsafe values', () => {
    const form = emptyPluginForm()
    form.name = 'Demo'
    form.requirements.push(
      {
        uid: 'r1',
        reqKey: 'api_key',
        description: '',
        required: true,
        serviceId: 'svc-1',
        credentialType: 'env',
      },
      {
        uid: 'r2',
        reqKey: 'api_key',
        description: '',
        required: true,
        serviceId: 'svc-2',
        credentialType: 'env',
      },
      {
        uid: 'r3',
        reqKey: 'bad key!',
        description: '',
        required: true,
        serviceId: 'svc-3',
        credentialType: 'env',
      },
    )
    form.mcps.push({
      uid: 'm1',
      name: 'Runner',
      transport: 'stdio',
      desktop: 'none',
      command: 'npx --yes; rm -rf',
      argsText: '',
      cwd: '/workspace',
      env: [
        { uid: 'e1', key: 'TOKEN', value: '{{credential.unknown_key}}' },
        { uid: 'e2', key: 'TOKEN', value: 'dup' },
        { uid: 'e3', key: 'OTHER', value: '{{env.TOKEN}}' },
      ],
      headers: [],
      url: '',
      startupTimeout: 30,
      requestTimeout: 60,
      authType: 'none',
      oauthRequirementKey: '',
    })
    const errors = validatePluginForm(form)
    expect(errors.some((e) => e.includes('duplicate key'))).toBe(true)
    expect(errors.some((e) => e.includes('stable identifier'))).toBe(true)
    expect(errors.some((e) => e.includes('single executable'))).toBe(true)
    expect(errors.some((e) => e.includes('unknown credential "unknown_key"'))).toBe(true)
    expect(errors.some((e) => e.includes('duplicate env key'))).toBe(true)
    expect(errors.some((e) => e.includes('unsupported placeholder'))).toBe(true)

    const urlForm = emptyPluginForm()
    urlForm.name = 'Demo'
    urlForm.mcps.push({
      uid: 'm1',
      name: 'Runner',
      transport: 'streamable_http',
      desktop: 'none',
      command: '',
      argsText: '',
      cwd: '/workspace',
      env: [],
      headers: [],
      url: 'https://user:pass@mcp.example.com/mcp#frag',
      startupTimeout: 30,
      requestTimeout: 60,
      authType: 'none',
      oauthRequirementKey: '',
    })
    expect(validatePluginForm(urlForm).some((e) => e.includes('userinfo or a fragment'))).toBe(true)
  })

  it('rejects names without identifier characters (empty derived slug)', () => {
    const form = emptyPluginForm()
    form.name = '!!!'
    expect(validatePluginForm(form).some((e) => e.includes('identifier can be derived'))).toBe(true)
    form.name = 'Demo'
    expect(validatePluginForm(form).filter((e) => e.includes('identifier'))).toEqual([])
  })
})

it('round-trips managed desktop policies and rejects explicit display conflicts', () => {
  const plugin = makePlugin()
  plugin.mcp_servers[0]!.resources = { desktop: {} }
  const form = pluginToForm(plugin)
  expect(form.mcps[0]!.desktop).toBe('server_start')
  form.mcps[0]!.desktop = 'first_tool'
  expect(formToCreateIn(form).mcp_servers?.[0]?.resources).toEqual({
    desktop: { activation: 'first_tool' },
  })
  form.mcps[0]!.env = [{ uid: 'display', key: 'DISPLAY', value: ':9' }]
  expect(validatePluginForm(form).join(' ')).toContain('remove those env entries')
  expect(formToCreateIn(form).mcp_servers?.[0]?.env).toEqual({ DISPLAY: ':9' })
  form.mcps[0]!.desktop = 'none'
  expect(formToCreateIn(form).mcp_servers?.[0]?.resources).toEqual({})
})
