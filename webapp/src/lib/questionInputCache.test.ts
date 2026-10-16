import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearQuestionInputCache,
  clearQuestionInputCaches,
  loadQuestionInputCache,
  pruneQuestionInputCaches,
  questionInputCacheKey,
  saveQuestionInputCache,
} from './questionInputCache'
import type { QuestionInputDraft } from './questionInputCache'

const identity = { userId: 17, organizationId: 'org-a' }
const request = {
  workspace_id: 'workspace-a',
  session_id: 'session-a',
  request_id: 'question/one ?&',
}
const draft: QuestionInputDraft = { answers: ['blue'], custom: ['manual'] }

function key(overrides: Partial<typeof request> = {}): string {
  return questionInputCacheKey(identity, { ...request, ...overrides })
}

describe('question input cache', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('isolates every scope id and URI-encodes the request id', () => {
    const baseKey = key()
    const otherKeys = [
      questionInputCacheKey({ userId: 18, organizationId: 'org-a' }, request),
      questionInputCacheKey({ userId: 17, organizationId: 'org-b' }, request),
      questionInputCacheKey(identity, { ...request, workspace_id: 'workspace-b' }),
      questionInputCacheKey(identity, { ...request, session_id: 'session-b' }),
      key({ request_id: 'question/two ?&' }),
    ]

    expect(baseKey).toContain('question-input:["17","org-a","workspace-a","session-a"]:')
    expect(baseKey.endsWith(encodeURIComponent(request.request_id))).toBe(true)
    expect(new Set([baseKey, ...otherKeys]).size).toBe(6)
    expect(questionInputCacheKey({ userId: '17', organizationId: 'org-a' }, request)).toBe(baseKey)
  })

  it('loads valid drafts and discards invalid drafts', () => {
    const cacheKey = key()
    window.sessionStorage.setItem(cacheKey, JSON.stringify(draft))

    expect(loadQuestionInputCache(cacheKey, 1)).toEqual(draft)

    const invalidKey = key({ request_id: 'invalid' })
    window.sessionStorage.setItem(
      invalidKey,
      JSON.stringify({ answers: ['one'], custom: ['one', 2] }),
    )
    expect(loadQuestionInputCache(invalidKey, 1)).toBeNull()
    expect(window.sessionStorage.getItem(invalidKey)).toBeNull()
  })

  it('ignores corrupt JSON and unavailable storage', () => {
    const cacheKey = key()
    window.sessionStorage.setItem(cacheKey, '{not json')
    expect(loadQuestionInputCache(cacheKey, 1)).toBeNull()

    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })

    expect(loadQuestionInputCache(cacheKey, 1)).toBeNull()
    expect(() => saveQuestionInputCache(cacheKey, draft)).not.toThrow()
    expect(() => clearQuestionInputCache(identity, request)).not.toThrow()
    expect(() => clearQuestionInputCaches()).not.toThrow()
  })

  it('deletes empty drafts but preserves whitespace-only free text', () => {
    const cacheKey = key()
    saveQuestionInputCache(cacheKey, draft)
    saveQuestionInputCache(cacheKey, { answers: ['', ''], custom: ['', ''] })
    expect(window.sessionStorage.getItem(cacheKey)).toBeNull()

    const whitespaceDraft = { answers: ['  \n'], custom: [''] }
    saveQuestionInputCache(cacheKey, whitespaceDraft)
    expect(loadQuestionInputCache(cacheKey, 1)).toEqual(whitespaceDraft)
  })

  it('prunes only absent requests in the authoritative session scope', () => {
    const pendingKey = key({ request_id: 'pending/request' })
    const staleKey = key({ request_id: 'stale/request' })
    const otherSessionKey = questionInputCacheKey(identity, {
      ...request,
      session_id: 'session-b',
      request_id: 'other-session',
    })
    const otherWorkspaceKey = questionInputCacheKey(identity, {
      ...request,
      workspace_id: 'workspace-b',
      request_id: 'other-workspace',
    })
    const otherOrgKey = questionInputCacheKey(
      { userId: 17, organizationId: 'org-b' },
      {
        ...request,
        request_id: 'other-org',
      },
    )
    const otherUserKey = questionInputCacheKey(
      { userId: 18, organizationId: 'org-a' },
      {
        ...request,
        request_id: 'other-user',
      },
    )

    for (const cacheKey of [
      pendingKey,
      staleKey,
      otherSessionKey,
      otherWorkspaceKey,
      otherOrgKey,
      otherUserKey,
    ]) {
      saveQuestionInputCache(cacheKey, draft)
    }

    pruneQuestionInputCaches(identity, request, ['pending/request'])

    expect(window.sessionStorage.getItem(pendingKey)).not.toBeNull()
    expect(window.sessionStorage.getItem(staleKey)).toBeNull()
    for (const cacheKey of [otherSessionKey, otherWorkspaceKey, otherOrgKey, otherUserKey]) {
      expect(window.sessionStorage.getItem(cacheKey)).not.toBeNull()
    }
  })

  it('clears question drafts without removing unrelated session data', () => {
    const cacheKey = key()
    window.sessionStorage.setItem('harness-chat:session-a', 'chat state')
    saveQuestionInputCache(cacheKey, draft)

    clearQuestionInputCaches()

    expect(window.sessionStorage.getItem(cacheKey)).toBeNull()
    expect(window.sessionStorage.getItem('harness-chat:session-a')).toBe('chat state')
  })
})
