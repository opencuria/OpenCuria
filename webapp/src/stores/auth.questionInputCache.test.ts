import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useAuthStore } from '@/stores/auth'
import { getMe } from '@/services/auth.api'
import {
  loadQuestionInputCache,
  questionInputCacheKey,
  saveQuestionInputCache,
} from '@/lib/questionInputCache'
import type { User, UserOrg, UserWithOrgs } from '@/types'

vi.mock('@/services/auth.api', async () => {
  const actual = await vi.importActual<typeof import('@/services/auth.api')>('@/services/auth.api')
  return { ...actual, getMe: vi.fn() }
})

const getMeMock = vi.mocked(getMe)
const user: User = {
  id: 17,
  email: 'user@example.com',
  first_name: 'Test',
  last_name: 'User',
}
const nextUser: User = { ...user, id: 18, email: 'new@example.com' }
const orgA: UserOrg = {
  id: 'org-a',
  name: 'Organization A',
  slug: 'org-a',
  role: 'admin',
  created_at: '2025-01-01T00:00:00Z',
}
const orgB: UserOrg = { ...orgA, id: 'org-b', name: 'Organization B', slug: 'org-b' }
const draft = { answers: ['blue'], custom: ['manual'] }

function cacheKey(owner: User, organizationId: string): string {
  return questionInputCacheKey(
    { userId: owner.id, organizationId },
    {
      workspace_id: 'workspace-a',
      session_id: 'session-a',
      request_id: 'question-a',
    },
  )
}

function seedQuestionDraft(key = cacheKey(user, orgA.id)): string {
  saveQuestionInputCache(key, draft)
  return key
}

describe('auth question input cache lifecycle', () => {
  beforeEach(() => {
    localStorage.clear()
    window.sessionStorage.clear()
    vi.clearAllMocks()
    setActivePinia(createPinia())
    getMeMock.mockResolvedValue({ ...user, organizations: [orgA, orgB] } satisfies UserWithOrgs)
  })

  it('clears question drafts on logout but leaves unrelated chat state intact', () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = orgA.id
    const key = seedQuestionDraft()
    window.sessionStorage.setItem('harness-chat:session-a', 'chat state')

    auth.logout()

    expect(loadQuestionInputCache(key, 1)).toBeNull()
    expect(window.sessionStorage.getItem('harness-chat:session-a')).toBe('chat state')
  })

  it('clears question drafts when the authenticated user changes', async () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = orgA.id
    const ownDraft = seedQuestionDraft()
    const otherUserDraft = seedQuestionDraft(cacheKey(nextUser, orgA.id))
    getMeMock.mockResolvedValueOnce({ ...nextUser, organizations: [orgA] })

    await auth.fetchMe()

    expect(auth.user?.id).toBe(nextUser.id)
    expect(loadQuestionInputCache(ownDraft, 1)).toBeNull()
    expect(loadQuestionInputCache(otherUserDraft, 1)).toBeNull()
  })

  it('notifies mounted provider catalogs after the new login context is stored', async () => {
    const auth = useAuthStore()
    auth.user = user
    auth.organizations = [orgA]
    auth.activeOrganizationId = orgA.id
    localStorage.setItem('kern_user', JSON.stringify(user))
    localStorage.setItem('kern_active_org_id', orgA.id)
    getMeMock.mockResolvedValueOnce({ ...nextUser, organizations: [orgA] })
    const contextAtNotification: string[] = []
    const onAccountsChanged = () => {
      contextAtNotification.push(localStorage.getItem('kern_user') ?? '')
    }
    window.addEventListener('harness:engine-connection-changed', onAccountsChanged)

    await auth.fetchMe()

    window.removeEventListener('harness:engine-connection-changed', onAccountsChanged)
    expect(contextAtNotification).toEqual([JSON.stringify(nextUser)])
  })

  it('keeps organization-scoped drafts when switching organizations', () => {
    const auth = useAuthStore()
    auth.user = user
    auth.organizations = [orgA, orgB]
    auth.activeOrganizationId = orgA.id
    const firstOrgDraft = seedQuestionDraft(cacheKey(user, orgA.id))
    const secondOrgDraft = seedQuestionDraft(cacheKey(user, orgB.id))

    auth.setActiveOrganization(orgB.id)

    expect(auth.activeOrganizationId).toBe(orgB.id)
    expect(loadQuestionInputCache(firstOrgDraft, 1)).toEqual(draft)
    expect(loadQuestionInputCache(secondOrgDraft, 1)).toEqual(draft)
    expect(firstOrgDraft).not.toBe(secondOrgDraft)
  })
})
