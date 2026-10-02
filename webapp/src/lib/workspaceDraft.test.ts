import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clearWorkspaceDrafts,
  getActiveWorkspaceDraftId,
  readWorkspaceDraft,
  saveWorkspaceDraft,
  WORKSPACE_DRAFT_TTL_MS,
} from './workspaceDraft'

const identity = { userId: 7, organizationId: 'org-a' }
const fields = {
  mode: 'create' as const,
  name: 'Build workspace',
  credentialIds: ['cred-1'],
  pluginIds: ['plugin-1'],
  repos: ['https://example.com/repo'],
  imageValue: 'definition:image-1',
}

beforeEach(() => sessionStorage.clear())
afterEach(() => clearWorkspaceDrafts())

describe('workspace draft persistence', () => {
  it('stores only typed configuration metadata and restores it across a recreated reader', () => {
    const saved = saveWorkspaceDraft(fields, identity, '/workspaces')
    expect(saved.error).toBeUndefined()
    expect(getActiveWorkspaceDraftId()).toBe(saved.id)
    const raw = sessionStorage.getItem(`opencuria:workspace-draft:${saved.id}`)!
    expect(raw).toContain('plugin-1')
    expect(raw).not.toContain('secret')
    expect(raw).not.toContain('token')
    expect(readWorkspaceDraft(saved.id, identity)).toMatchObject({
      status: 'ok',
      draft: { fields, returnPath: '/workspaces' },
    })
  })

  it('rejects invalid numeric bounds, duplicate references, and malformed shapes on save', () => {
    expect(saveWorkspaceDraft({ ...fields, qemuVcpus: 0 }, identity, '/').error).toContain(
      'invalid configuration',
    )
    expect(
      saveWorkspaceDraft({ ...fields, pluginIds: ['plugin-1', 'plugin-1'] }, identity, '/').error,
    ).toContain('invalid configuration')
    expect(
      saveWorkspaceDraft({ ...fields, workspaceId: 'workspace-1' }, identity, 'https://evil.test')
        .error,
    ).toBeTruthy()
  })

  it('rejects expired, identity-mismatched, malformed and absent drafts', () => {
    const saved = saveWorkspaceDraft(fields, identity, '/')
    const key = `opencuria:workspace-draft:${saved.id}`
    const item = JSON.parse(sessionStorage.getItem(key)!)
    item.createdAt = Date.now() - WORKSPACE_DRAFT_TTL_MS - 1
    sessionStorage.setItem(key, JSON.stringify(item))
    expect(readWorkspaceDraft(saved.id, identity).status).toBe('expired')
    const next = saveWorkspaceDraft(fields, identity, '/')
    expect(readWorkspaceDraft(next.id, { userId: 8, organizationId: 'org-a' }).status).toBe(
      'identity-mismatch',
    )
    const malformedId = 'malformed'
    sessionStorage.setItem(`opencuria:workspace-draft:${malformedId}`, '{')
    expect(readWorkspaceDraft(malformedId, identity).status).toBe('invalid')
    expect(readWorkspaceDraft('missing', identity).status).toBe('missing')
  })

  it('refuses arbitrary routes, untyped fields, and anonymous identities', () => {
    expect(saveWorkspaceDraft(fields, identity, 'https://evil.test').error).toBeTruthy()
    expect(
      saveWorkspaceDraft({ ...fields, secret: 'never persist' } as never, identity, '/').error,
    ).toContain('invalid configuration')
    expect(sessionStorage.length).toBe(0)
    expect(saveWorkspaceDraft(fields, { userId: '', organizationId: '' }, '/').error).toContain(
      'Sign in',
    )
  })
})
