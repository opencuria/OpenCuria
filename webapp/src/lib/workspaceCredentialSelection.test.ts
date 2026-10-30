import { describe, expect, it } from 'vitest'

import { toggleWorkspaceCredentialSelection } from './workspaceCredentialSelection'

describe('workspaceCredentialSelection', () => {
  it('replaces the existing selection for the same service', () => {
    const credentials = [
      { id: 'cred-1', service_id: 'github' },
      { id: 'cred-2', service_id: 'github' },
      { id: 'cred-3', service_id: 'openai' },
    ]

    expect(
      toggleWorkspaceCredentialSelection(['cred-1', 'cred-3'], credentials[1]!, credentials),
    ).toEqual(['cred-3', 'cred-2'])
  })

  it('removes a credential when it is toggled off', () => {
    const selected = { id: 'cred-1', service_id: 'github' }

    expect(toggleWorkspaceCredentialSelection(['cred-1'], selected, [selected])).toEqual([])
  })
})
