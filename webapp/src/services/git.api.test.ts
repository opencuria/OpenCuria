import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiRequestError, get, post } from './api'
import {
  buildGitOperationBody,
  conflictSnapshotOf,
  getGitCommitDetails,
  getGitSnapshot,
  getGitWorkingDiff,
  runGitOperation,
  type RawGitRepoSnapshot,
} from './git.api'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    get: vi.fn(),
    post: vi.fn(),
  }
})

const getMock = vi.mocked(get)
const postMock = vi.mocked(post)

function makeSnapshot(overrides: Partial<RawGitRepoSnapshot> = {}): RawGitRepoSnapshot {
  return {
    id: '/workspace/repo',
    name: 'repo',
    path: '/workspace/repo',
    current_branch: 'main',
    head_hash: 'abc1234',
    branches: [],
    remote_refs: [],
    remotes: ['origin'],
    default_remote: 'refs/remotes/origin/HEAD',
    upstream: 'origin/main',
    ahead: 1,
    behind: 0,
    merge_state: { merging: false, rebasing: false, cherry_picking: false },
    commits: [],
    has_more: false,
    history_skip: 0,
    history_limit: 200,
    changes: [],
    ...overrides,
  }
}

describe('git.api', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('builds the snapshot URL with history paging params', async () => {
    getMock.mockResolvedValue({ ok: true, repos: [] })
    await getGitSnapshot('ws-1')
    expect(getMock).toHaveBeenCalledWith(
      '/workspaces/ws-1/git/?history_limit=200&history_skip=0',
    )
  })

  it('forwards custom history paging and encodes the workspace id', async () => {
    getMock.mockResolvedValue({ ok: true, repos: [] })
    await getGitSnapshot('ws/1', { historyLimit: 50, historySkip: 10 })
    expect(getMock).toHaveBeenCalledWith(
      '/workspaces/ws%2F1/git/?history_limit=50&history_skip=10',
    )
  })

  it('encodes repo_path for the working diff endpoint', async () => {
    getMock.mockResolvedValue({ ok: true, repo_path: '/workspace/repo', diff: { staged: [], unstaged: [] } })
    await getGitWorkingDiff('ws-1', '/workspace/my repo')
    const url = getMock.mock.calls[0]![0] as string
    expect(url.startsWith('/workspaces/ws-1/git/diff/?')).toBe(true)
    expect(url).toContain('repo_path=%2Fworkspace%2Fmy+repo')
  })

  it('encodes repo_path and commit hash for the commit details endpoint', async () => {
    getMock.mockResolvedValue({ ok: true, repo_path: '/workspace/repo', details: {} })
    await getGitCommitDetails('ws-1', '/workspace/repo', 'ABC123')
    const url = getMock.mock.calls[0]![0] as string
    expect(url.startsWith('/workspaces/ws-1/git/commits/ABC123/?')).toBe(true)
    expect(url).toContain('repo_path=%2Fworkspace%2Frepo')
  })

  it('posts typed operation bodies without undefined fields', async () => {
    postMock.mockResolvedValue({ ok: true, snapshot: makeSnapshot() })
    await runGitOperation('ws-1', {
      operation: 'stage',
      repo_path: '/workspace/repo',
      paths: ['a.txt'],
    })
    expect(postMock).toHaveBeenCalledWith('/workspaces/ws-1/git/operation/', {
      operation: 'stage',
      repo_path: '/workspace/repo',
      paths: ['a.txt'],
    })
  })

  it('builds create_branch bodies with start_point and checkout flags', () => {
    expect(
      buildGitOperationBody({
        operation: 'create_branch',
        repo_path: '/workspace/repo',
        branch: 'feature/x',
        start_point: 'main',
        checkout: true,
      }),
    ).toEqual({
      operation: 'create_branch',
      repo_path: '/workspace/repo',
      branch: 'feature/x',
      start_point: 'main',
      checkout: true,
    })
  })

  it('keeps 409 conflict payloads (snapshot + message) on ApiRequestError.data', async () => {
    const snapshot = makeSnapshot({ path: '/workspace/repo' })
    const conflictBody = {
      ok: false,
      code: 'conflict',
      message: 'Merge conflict — resolve or run merge_abort',
      stderr: 'CONFLICT (content)',
      snapshot,
    }
    postMock.mockRejectedValue(new ApiRequestError(409, conflictBody.message, 'conflict', conflictBody))

    const failure = await runGitOperation('ws-1', {
      operation: 'merge_into_current',
      repo_path: '/workspace/repo',
      branch: 'feature/x',
    }).catch((e: unknown) => e)

    expect(failure).toBeInstanceOf(ApiRequestError)
    const err = failure as ApiRequestError
    expect(err.status).toBe(409)
    expect(err.code).toBe('conflict')
    expect(err.message).toBe('Merge conflict — resolve or run merge_abort')
    expect(conflictSnapshotOf(err.data)).toEqual(snapshot)
  })

  it('rejects non-snapshot shapes from conflictSnapshotOf', () => {
    expect(conflictSnapshotOf(null)).toBeNull()
    expect(conflictSnapshotOf({ ok: false })).toBeNull()
    expect(conflictSnapshotOf({ snapshot: { repos: [] } })).toBeNull()
    expect(conflictSnapshotOf({ snapshot: makeSnapshot() })).toEqual(makeSnapshot())
  })
})
