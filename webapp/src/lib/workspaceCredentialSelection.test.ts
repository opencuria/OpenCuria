import { describe, expect, it } from 'vitest'

import type { Credential } from '@/types'

import {
  groupWorkspaceCredentials,
  toggleWorkspaceCredentialSelection,
} from './workspaceCredentialSelection'

function makeCredential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: overrides.id ?? 'cred-1',
    name: overrides.name ?? 'Credential',
    scope: overrides.scope ?? 'personal',
    service_id: overrides.service_id ?? 'service-1',
    service_name: overrides.service_name ?? 'GitHub',
    service_slug: overrides.service_slug ?? 'github',
    credential_type: overrides.credential_type ?? 'env',
    env_var_name: overrides.env_var_name ?? 'GITHUB_TOKEN',
    target_path: overrides.target_path ?? '',
    has_public_key: overrides.has_public_key ?? false,
    created_by_id: overrides.created_by_id ?? 1,
    created_at: overrides.created_at ?? '2026-04-18T00:00:00.000Z',
    updated_at: overrides.updated_at ?? '2026-04-18T00:00:00.000Z',
  }
}

describe('workspaceCredentialSelection', () => {
  it('replaces the existing selection for the same service', () => {
    const credentials = [
      makeCredential({ id: 'cred-1', name: 'Personal GitHub', service_id: 'github' }),
      makeCredential({ id: 'cred-2', name: 'Org GitHub', service_id: 'github' }),
      makeCredential({
        id: 'cred-3',
        name: 'OpenAI',
        service_id: 'openai',
        service_name: 'OpenAI',
        service_slug: 'openai',
        env_var_name: 'OPENAI_API_KEY',
      }),
    ]

    const replacement = credentials[1]!

    expect(
      toggleWorkspaceCredentialSelection(['cred-1', 'cred-3'], replacement, credentials),
    ).toEqual(['cred-3', 'cred-2'])
  })

  it('removes a credential when it is toggled off', () => {
    const credentials = [makeCredential({ id: 'cred-1' })]
    const selected = credentials[0]!

    expect(
      toggleWorkspaceCredentialSelection(['cred-1'], selected, credentials),
    ).toEqual([])
  })

  it('groups credentials by service and tracks the active selection', () => {
    const credentials = [
      makeCredential({ id: 'cred-2', name: 'Org GitHub', service_id: 'github' }),
      makeCredential({ id: 'cred-1', name: 'Personal GitHub', service_id: 'github' }),
      makeCredential({
        id: 'cred-3',
        name: 'Codex Auth',
        service_id: 'codex',
        service_name: 'Codex',
        service_slug: 'codex',
        credential_type: 'file',
        env_var_name: '',
        target_path: '~/.codex/auth.json',
      }),
    ]

    expect(groupWorkspaceCredentials(credentials, ['cred-2'])).toEqual([
      {
        serviceId: 'codex',
        serviceName: 'Codex',
        credentialType: 'file',
        envVarName: '',
        targetPath: '~/.codex/auth.json',
        selectedCredentialId: null,
        credentials: [credentials[2]],
      },
      {
        serviceId: 'github',
        serviceName: 'GitHub',
        credentialType: 'env',
        envVarName: 'GITHUB_TOKEN',
        targetPath: '',
        selectedCredentialId: 'cred-2',
        credentials: [credentials[0], credentials[1]],
      },
    ])
  })
})
