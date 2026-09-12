/**
 * Shared productive git fixtures for store + component tests.
 *
 * Builds snake_case backend payloads and exposes both the raw shapes and
 * the normalized domain expectations. Mocking happens at the `git.api`
 * boundary (`vi.mock('@/services/git.api')`) — never against the real API.
 */

import { vi, type Mock } from 'vitest'
import type {
  RawGitChange,
  RawGitCommit,
  RawGitCommitDetails,
  RawGitCommitFile,
  RawGitDiffHunk,
  RawGitRepoSnapshot,
  RawGitRepoSummary,
} from '@/services/git.api'

export function makeHunk(suffix: string): RawGitDiffHunk {
  return {
    header: `@@ -1,2 +1,2 @@ ${suffix}`,
    old_start: 1,
    new_start: 1,
    lines: [
      { type: 'context', content: `// ${suffix}` },
      { type: 'del', content: 'const before = 1' },
      { type: 'add', content: 'const after = 2' },
    ],
  }
}

export function makeCommitFile(
  path: string,
  overrides: Partial<RawGitCommitFile> = {},
): RawGitCommitFile {
  return {
    old_path: path,
    new_path: path,
    status: 'M',
    additions: 3,
    deletions: 1,
    binary: false,
    truncated: false,
    has_textual_diff: true,
    diff: [makeHunk(path)],
    ...overrides,
  }
}

export function makeRawCommit(
  hash: string,
  overrides: Partial<RawGitCommit> = {},
): RawGitCommit {
  return {
    hash,
    message: `commit ${hash}`,
    body: '',
    author: 'Timo Kamphaus',
    author_email: 'timo@opencuria.local',
    timestamp: `2026-09-0${(hash.charCodeAt(0) % 8) + 1}T10:00:00Z`,
    author_date: `2026-09-0${(hash.charCodeAt(0) % 8) + 1}T10:00:00Z`,
    committer: 'Timo Kamphaus',
    committer_email: 'timo@opencuria.local',
    committer_date: `2026-09-0${(hash.charCodeAt(0) % 8) + 1}T10:00:00Z`,
    parents: [],
    ...overrides,
  }
}

export function makeRawChange(
  path: string,
  overrides: Partial<RawGitChange> = {},
): RawGitChange {
  return {
    path,
    old_path: null,
    status: 'M',
    staged: false,
    staged_kind: null,
    unstaged: 'M',
    conflict: null,
    diff: [],
    ...overrides,
  }
}

export interface RepoFixtureOptions {
  id?: string
  name?: string
  path?: string
  currentBranch?: string | null
  headHash?: string | null
  commits?: RawGitCommit[]
  changes?: RawGitChange[]
  hasMore?: boolean
  branches?: RawGitRepoSnapshot['branches']
  mergeState?: Partial<RawGitRepoSnapshot['merge_state']>
  remotes?: string[]
  defaultRemote?: string | null
}

export function makeRepoSnapshot(options: RepoFixtureOptions = {}): RawGitRepoSnapshot {
  const path = options.path ?? '/workspace/repo-app'
  const head = options.headHash === undefined ? 'f4a9c21' : options.headHash
  return {
    id: options.id ?? path,
    name: options.name ?? path.split('/').pop() ?? 'repo-app',
    path,
    current_branch: options.currentBranch === undefined ? 'main' : options.currentBranch,
    head_hash: head,
    branches:
      options.branches ??
      [
        { name: 'main', tip_hash: head ?? 'f4a9c21', upstream: 'origin/main', ahead: 2, behind: 0 },
        { name: 'feature/git-panel', tip_hash: 'g5h1k83', upstream: null, ahead: 0, behind: 0 },
      ],
    remote_refs: [{ name: 'origin/main', tip_hash: '9c2f1e7' }],
    remotes: options.remotes ?? ['origin'],
    default_remote:
      options.defaultRemote === undefined ? 'refs/remotes/origin/HEAD' : options.defaultRemote,
    upstream: 'origin/main',
    ahead: 2,
    behind: 0,
    merge_state: {
      merging: options.mergeState?.merging === true,
      rebasing: options.mergeState?.rebasing === true,
      cherry_picking: options.mergeState?.cherry_picking === true,
    },
    commits:
      options.commits ??
      [
        // All-branch history (like vscode-git-graph): the backend delivers
        // commits across every ref, so both branch tips are present.
        makeRawCommit('f4a9c21', { message: 'Fix terminal resize flicker', parents: ['e8b7d3a'] }),
        makeRawCommit('e8b7d3a', { message: 'Polish settings sheet spacing', parents: ['9c2f1e7'] }),
        makeRawCommit('9c2f1e7', {
          message: "Merge branch 'feature/login-form'",
          parents: ['3d8e5b2', 'b7c4a19'],
        }),
        makeRawCommit('g5h1k83', { message: 'Add git panel layout', parents: ['9c2f1e7'] }),
      ],
    has_more: options.hasMore ?? false,
    history_skip: 0,
    history_limit: 50,
    changes:
      options.changes ??
      [
        makeRawChange('webapp/src/stores/git.ts', {
          status: 'A',
          staged: true,
          staged_kind: 'A',
          unstaged: null,
        }),
        makeRawChange('webapp/src/lib/gitGraph.ts', {
          status: 'M',
          staged: false,
          staged_kind: null,
          unstaged: 'M',
        }),
      ],
  }
}

export function makeCommitDetails(
  hash: string,
  overrides: Partial<RawGitCommitDetails> = {},
): RawGitCommitDetails {
  return {
    hash,
    message: `commit ${hash}`,
    body: '',
    parents: ['parent-1'],
    author: 'Timo Kamphaus',
    author_email: 'timo@opencuria.local',
    author_date: '2026-09-05T10:00:00Z',
    committer: 'Timo Kamphaus',
    committer_email: 'timo@opencuria.local',
    committer_date: '2026-09-05T10:00:00Z',
    file_changes: [makeCommitFile('webapp/src/stores/git.ts')],
    ...overrides,
  }
}

export const mockedGitApiPath = '@/services/git.api'

export function makeRepoSummary(
  path = '/workspace/repo-app',
  overrides: Partial<RawGitRepoSummary> = {},
): RawGitRepoSummary {
  const { id, name, current_branch, head_hash, ...rest } = overrides
  void rest
  return {
    id: id ?? path,
    name: name ?? path.split('/').pop() ?? 'repo-app',
    path,
    current_branch: current_branch === undefined ? 'main' : current_branch,
    head_hash: head_hash === undefined ? 'f4a9c21' : head_hash,
  }
}

/**
 * Wire summaries + detail snapshots + paged history for the lazy git API
 * (replaces the old `getGitSnapshot.mockResolvedValue({ok, repos})`).
 * Accepts raw snapshots; summaries are derived automatically and
 * getGitHistory slices the fixture commits.
 */
export function setupGitRepos(
  getRepos: Mock,
  getRepo: Mock,
  getHistory: Mock,
  repos: RawGitRepoSnapshot[],
): void {
  getRepos.mockResolvedValue({
    ok: true,
    repos: repos.map(
      (r): RawGitRepoSummary => ({
        id: r.id,
        name: r.name,
        path: r.path,
        current_branch: r.current_branch,
        head_hash: r.head_hash,
      }),
    ),
  })
  getRepo.mockImplementation(async (_ws: string, repoPath: string) => {
    const found = repos.find((r) => r.path === repoPath) ?? repos[0] ?? makeRepoSnapshot()
    return { ok: true, snapshot: { ...found } }
  })
  getHistory.mockImplementation(async (_ws: string, repoPath: string, opts?: { skip?: number; limit?: number }) => {
    const found = repos.find((r) => r.path === repoPath) ?? repos[0] ?? makeRepoSnapshot()
    const skip = opts?.skip ?? 0
    const limit = opts?.limit ?? 50
    const commits = (found.commits ?? []).slice(skip, skip + limit)
    return {
      ok: true,
      repo_path: repoPath,
      commits,
      has_more: (found.commits ?? []).length > skip + commits.length || found.has_more,
      history_skip: skip,
      history_limit: limit,
    }
  })
}

/**
 * Install the default `git.api` mock (summaries + one detail snapshot).
 * Returns the mocked functions for per-test overrides.
 */export async function mockGitApi(repos: RawGitRepoSnapshot[] = [makeRepoSnapshot()]) {
  const mod = await import('@/services/git.api')
  const mocked = vi.mocked(mod, true)
  mocked.getGitRepos.mockResolvedValue({
    ok: true,
    repos: repos.map((r) => ({
      id: r.id,
      name: r.name,
      path: r.path,
      current_branch: r.current_branch,
      head_hash: r.head_hash,
    })),
  })
  mocked.getGitRepo.mockImplementation(async (_ws, repoPath) => {
    const found = repos.find((r) => r.path === repoPath) ?? repos[0] ?? makeRepoSnapshot()
    return { ok: true, snapshot: { ...found } }
  })
  mocked.getGitHistory.mockImplementation(async (_ws, repoPath, opts) => {
    const found = repos.find((r) => r.path === repoPath) ?? repos[0] ?? makeRepoSnapshot()
    const skip = opts?.skip ?? 0
    const limit = opts?.limit ?? 50
    const commits = (found.commits ?? []).slice(skip, skip + limit)
    return {
      ok: true,
      repo_path: repoPath,
      commits,
      has_more: (found.commits ?? []).length > skip + commits.length || found.has_more,
      history_skip: skip,
      history_limit: limit,
    }
  })
  mocked.getGitWorkingDiff.mockResolvedValue({
    ok: true,
    repo_path: repos[0]?.path ?? '/workspace/repo-app',
    diff: { staged: [], unstaged: [] },
  })
  mocked.getGitCommitDetails.mockImplementation(async (_ws, _repo, hash) => ({
    ok: true,
    repo_path: repos[0]?.path ?? '/workspace/repo-app',
    details: makeCommitDetails(hash),
  }))
  mocked.runGitOperation.mockImplementation(async (_ws, payload) => ({
    ok: true,
    snapshot: { ...(repos[0] ?? makeRepoSnapshot()) },
    repo_path: repos[0]?.path ?? '/workspace/repo-app',
    operation: payload.operation,
  }) as never)
  return mocked
}
