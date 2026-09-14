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
        plugin_owned_service: true,
      },
    ],
    credential_readiness: {
      required_service_ids: ['svc-1'],
      missing_required_service_ids: ['svc-1'],
      ready: false,
    },
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
    expect(rowsToDict([{ uid: 'a', key: 'K', value: 'v' }, { uid: 'b', key: ' ', value: 'x' }])).toEqual({
      K: 'v',
    })
  })

  it('round-trips a plugin to a form via service_id references', () => {
    const form = pluginToForm(makePlugin())
    expect(form.skills).toHaveLength(1)
    expect(form.mcps[0]?.argsText).toBe('-y\n@playwright/mcp@latest')
    expect(form.mcps[0]?.env[0]).toMatchObject({ key: 'TOKEN', value: '{{credential.API_KEY}}' })
    expect(form.requirements[0]?.mode).toBe('existing')
    expect(form.requirements[0]?.serviceId).toBe('svc-1')

    const update = formToUpdateIn(form)
    expect(update.credential_requirements?.[0]?.credential_service).toEqual({
      service_id: 'svc-1',
    })
    expect(update.skills?.[0]?.position).toBe(0)
    expect(update.mcp_servers?.[0]?.command).toBe('npx')
  })

  it('serializes new-service requirements with normalized fields', () => {
    const form = emptyPluginForm()
    form.name = 'Demo'
    form.requirements.push({
      uid: 'r1',
      reqKey: 'api_key',
      description: '',
      required: true,
      mode: 'new',
      serviceId: '',
      serviceName: 'Demo Auth',
      serviceSlug: '',
      credentialType: 'env',
      envVarName: 'demo_token',
      targetPath: '',
      label: '',
    })
    const create = formToCreateIn(form)
    expect(create.slug).toBe('')
    expect(create.credential_requirements?.[0]?.credential_service.env_var_name).toBe('DEMO_TOKEN')
  })

  it('validates required fields, transports, and requirement sources', () => {
    const form = emptyPluginForm()
    expect(validatePluginForm(form)).toContain('Plugin name is required.')

    form.name = 'Demo'
    form.mcps.push({
      uid: 'm1',
      name: 'Runner',
      slug: '',
      transport: 'stdio',
      command: '',
      argsText: '',
      cwd: '/workspace',
      env: [],
      headers: [],
      url: 'https://x.example',
      startupTimeout: 0,
      requestTimeout: 700,
    })
    form.requirements.push({
      uid: 'r1',
      reqKey: 'api_key',
      description: '',
      required: true,
      mode: 'existing',
      serviceId: '',
      serviceName: '',
      serviceSlug: '',
      credentialType: 'env',
      envVarName: '',
      targetPath: '',
      label: '',
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
      mode: 'existing',
      serviceId: 'svc-1',
      serviceName: '',
      serviceSlug: '',
      credentialType: 'env',
      envVarName: '',
      targetPath: '',
      label: '',
    })
    form.mcps.push({
      uid: 'm1',
      name: 'Runner',
      slug: '',
      transport: 'stdio',
      command: 'npx',
      argsText: '-y',
      cwd: '/workspace',
      env: [{ uid: 'e1', key: 'TOKEN', value: '{{credential.api_key}}' }],
      // Stale http fields must be dropped for stdio.
      headers: [{ uid: 'h1', key: 'Authorization', value: 'Bearer x' }],
      url: 'https://stale.example/mcp',
      startupTimeout: 30,
      requestTimeout: 60,
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
    form.mcps[0]!.headers = [{ uid: 'h1', key: 'Authorization', value: 'Bearer {{credential.api_key}}' }]
    const http = formToCreateIn(form).mcp_servers?.[0]
    expect(http?.command).toBe('')
    expect(http?.args).toEqual([])
    expect(http?.env).toEqual({})
    expect(http?.headers).toEqual({ Authorization: 'Bearer {{credential.api_key}}' })
  })

  it('rejects unknown placeholders, bad templates, duplicates, and unsafe values', () => {
    const form = emptyPluginForm()
    form.name = 'Demo'
    form.slug = 'Bad Slug!'
    form.slugTouched = true
    expect(validatePluginForm(form).some((e) => e.includes('slug must be URL-safe'))).toBe(true)
    form.slug = ''
    form.slugTouched = false
    form.requirements.push(
      {
        uid: 'r1', reqKey: 'api_key', description: '', required: true, mode: 'existing',
        serviceId: 'svc-1', serviceName: '', serviceSlug: '', credentialType: 'env', envVarName: '', targetPath: '', label: '',
      },
      {
        uid: 'r2', reqKey: 'api_key', description: '', required: true, mode: 'existing',
        serviceId: 'svc-2', serviceName: '', serviceSlug: '', credentialType: 'env', envVarName: '', targetPath: '', label: '',
      },
      {
        uid: 'r3', reqKey: 'bad key!', description: '', required: true, mode: 'existing',
        serviceId: 'svc-3', serviceName: '', serviceSlug: '', credentialType: 'env', envVarName: '', targetPath: '', label: '',
      },
    )
    form.mcps.push({
      uid: 'm1',
      name: 'Runner',
      slug: '',
      transport: 'stdio',
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
      uid: 'm1', name: 'Runner', slug: '', transport: 'streamable_http', command: '', argsText: '',
      cwd: '/workspace', env: [], headers: [],
      url: 'https://user:pass@mcp.example.com/mcp#frag',
      startupTimeout: 30, requestTimeout: 60,
    })
    expect(validatePluginForm(urlForm).some((e) => e.includes('userinfo or a fragment'))).toBe(true)
  })
})
