import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from './auth'
import { useEditorStore } from './editor'
import { getMe } from '@/services/auth.api'
import type { UserOrg } from '@/types'

vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(async (_workspace: string, path: string) => ({
    path,
    content: 'private text',
    revision: 'revision',
  })),
}))
vi.mock('@/services/auth.api', async () => {
  const actual = await vi.importActual<typeof import('@/services/auth.api')>('@/services/auth.api')
  return { ...actual, getMe: vi.fn() }
})

const user = { id: 17, email: 'user@example.com', first_name: '', last_name: '' }
const org: UserOrg = { id: 'org-a', name: 'A', slug: 'a', role: 'admin', created_at: '' }
const nextOrg: UserOrg = { ...org, id: 'org-b' }

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('editor authentication boundaries', () => {
  it.each(['logout', 'organization', 'user', 'membership'] as const)(
    'clears private buffers on %s changes',
    async (change) => {
      const auth = useAuthStore()
      auth.user = user
      auth.organizations = [org, nextOrg]
      auth.activeOrganizationId = org.id
      const editor = useEditorStore()
      await editor.openFile('workspace-a', '/workspace/private.txt')
      editor.updateContent(editor.activeTab!.id, 'private draft')
      expect(editor.hasUnsavedChanges).toBe(true)

      if (change === 'logout') auth.logout()
      else if (change === 'organization') auth.setActiveOrganization(nextOrg.id)
      else {
        vi.mocked(getMe).mockResolvedValueOnce({
          ...(change === 'user' ? { ...user, id: 18 } : user),
          organizations: change === 'membership' ? [nextOrg] : [org],
        })
        await auth.fetchMe()
      }

      editor.setWorkspace('workspace-a')
      expect(editor.tabs).toEqual([])
      expect(editor.hasUnsavedChanges).toBe(false)
    },
  )
})
