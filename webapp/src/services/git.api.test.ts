import { afterEach, expect, it, vi } from 'vitest'
import { ApiRequestError } from './api'
import { getGitRepos, readGitFileContent, runGitOperation } from './git.api'

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('uses the current web auth context and forwards editor cancellation through the shared client', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}'))
  vi.stubGlobal('fetch', fetchMock)
  localStorage.setItem('kern_access_token', 'access-token')
  localStorage.setItem('kern_active_org_id', 'org-a')
  await getGitRepos('ws')

  localStorage.setItem('kern_active_org_id', 'org-b')
  const controller = new AbortController()
  fetchMock.mockImplementationOnce(() => new Promise(() => undefined))
  const pending = readGitFileContent('ws', '/workspace/repo', 'a.txt', 'index', controller.signal)
  const cancelled = expect(pending).rejects.toMatchObject({ name: 'AbortError' })

  expect(
    fetchMock.mock.calls.map(([, init]) => {
      const headers = new Headers(init?.headers)
      return [headers.get('Authorization'), headers.get('X-Organization-Id'), init?.credentials]
    }),
  ).toEqual([
    ['Bearer access-token', 'org-a', 'same-origin'],
    ['Bearer access-token', 'org-b', 'same-origin'],
  ])
  controller.abort()
  await cancelled
  expect(fetchMock.mock.lastCall?.[1]?.signal?.aborted).toBe(true)
})

it('propagates denied Git operations through the web error type', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: 'Permission denied', code: 'permission_denied' }), {
          status: 403,
        }),
      ),
  )
  await expect(
    runGitOperation('ws', {
      operation: 'stage',
      repo_path: '/workspace/repo',
      paths: ['a.txt'],
    }),
  ).rejects.toMatchObject({
    constructor: ApiRequestError,
    status: 403,
    code: 'permission_denied',
  })
})
