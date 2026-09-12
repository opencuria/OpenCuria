import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { ApiRequestError } from '@/services/api'
import * as gitApi from '@/services/git.api'
import { useGitStore, selectedRepoStorageKey } from './git'
import {
  makeCommitDetails,
  makeCommitFile,
  makeRawChange,
  makeRawCommit,
  makeRepoSnapshot,
  makeRepoSummary,
} from './git.fixtures'
import type { RawGitRepoSnapshot } from '@/services/git.api'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/services/git.api', () => ({
  conflictSnapshotOf: vi.fn((data: unknown) => {
    const snapshot = (data as { snapshot?: unknown } | null)?.snapshot
    return snapshot && typeof snapshot === 'object' ? snapshot : null
  }),
  getGitCommitDetails: vi.fn(),
  getGitHistory: vi.fn(),
  getGitRepo: vi.fn(),
  getGitRepos: vi.fn(),
  getGitWorkingDiff: vi.fn(),
  runGitOperation: vi.fn(),
}))

const getRepos = vi.mocked(gitApi.getGitRepos)
const getRepo = vi.mocked(gitApi.getGitRepo)
const getHistory = vi.mocked(gitApi.getGitHistory)
const getDiff = vi.mocked(gitApi.getGitWorkingDiff)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)
const runOp = vi.mocked(gitApi.runGitOperation)

/** Point all three read endpoints at the given raw snapshots. */
function setRepos(repos: RawGitRepoSnapshot[]): void {
  getRepos.mockResolvedValue({
    ok: true,
    repos: repos.map((r) => ({
      id: r.id,
      name: r.name,
      path: r.path,
      current_branch: r.current_branch,
      head_hash: r.head_hash,
    })),
  })
  getRepo.mockImplementation(async (_ws, repoPath) => {
    const found = repos.find((r) => r.path === repoPath) ?? repos[0]!
    return { ok: true, snapshot: { ...found } }
  })
  getHistory.mockImplementation(async (_ws, repoPath, opts) => {
    const found = repos.find((r) => r.path === repoPath) ?? repos[0]!
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

async function initWith(repos = [makeRepoSnapshot()]) {
  setRepos(repos)
  const store = useGitStore()
  await store.initialize('ws-1')
  return store
}

function opPayload() {
  const calls = runOp.mock.calls
  return calls[calls.length - 1]?.[1]
}

/** Wait for the fire-and-forget details load after a sync `selectRepo()`. */
async function flushDetails(): Promise<void> {
  await vi.waitFor(() => {
    expect(getRepo).toHaveBeenCalled()
  })
  await Promise.resolve()
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('git store (productive)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    setRepos([makeRepoSnapshot()])
    getDiff.mockResolvedValue({ ok: true, repo_path: '/workspace/repo-app', diff: { staged: [], unstaged: [] } })
    getDetails.mockImplementation(async (_ws, repo, hash) => ({
      ok: true,
      repo_path: repo,
      details: makeCommitDetails(hash),
    }))
    runOp.mockImplementation(async (_ws, payload) => ({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
      operation: payload.operation,
    }) as never)
  })

  it('starts empty and loads summaries then details (normalized)', async () => {
    const store = useGitStore()
    expect(store.repos).toEqual([])
    expect(store.currentRepo).toBeNull()
    expect(store.loading).toBe(false)

    const second = makeRepoSnapshot({
      id: '/workspace/docs',
      path: '/workspace/docs',
      name: 'docs',
      headHash: 'd1o2c3s',
    })
    setRepos([makeRepoSnapshot(), second])
    await store.initialize('ws-1')

    expect(getRepos).toHaveBeenCalledWith('ws-1')
    expect(getRepo).toHaveBeenCalledWith('ws-1', '/workspace/repo-app')
    expect(store.repos).toHaveLength(2)
    expect(store.repos[0]).toMatchObject({
      id: '/workspace/repo-app',
      name: 'repo-app',
      path: '/workspace/repo-app',
      currentBranch: 'main',
      headHash: 'f4a9c21',
    })
    expect(store.currentRepo?.id).toBe('/workspace/repo-app')
    expect(store.currentBranch?.name).toBe('main')
    expect(store.currentBranch?.upstream).toBe('origin/main')
    // Snake_case normalization in details.
    expect(store.repoDetails['/workspace/repo-app']?.headHash).toBe('f4a9c21')
    expect(store.repoDetails['/workspace/repo-app']?.branches[0]).toMatchObject({ name: 'main', tipHash: 'f4a9c21', ahead: 2 })
    expect(store.repoDetails['/workspace/repo-app']?.changes[0]).toMatchObject({
      path: 'webapp/src/stores/git.ts',
      stagedKind: 'A',
      unstaged: null,
      conflict: null,
    })
    expect(store.error).toBeNull()
  })

  it('exposes a shallow currentRepo until details arrive', async () => {
    const store = useGitStore()
    await store.initialize('ws-1')
    expect(store.repos).toHaveLength(1)
    expect(store.currentRepo?.commits.length).toBeGreaterThan(0)

    // Simulate a second workspace with summaries only: drop details to
    // get the shallow projection back.
    getRepos.mockResolvedValue({ ok: true, repos: [makeRepoSummary()] })
    // Details never resolve for this repo.
    getRepo.mockImplementation(() => new Promise(() => {}))
    const before = store.currentRepo
    expect(before).not.toBeNull()
    store.repoDetails = {}
    await store.refresh({ withDetails: false })

    expect(store.repos).toHaveLength(1)
    expect(store.repoDetails['/workspace/repo-app']).toBeUndefined()
    // Shallow projection: summary branch/head, empty details, hasMore true.
    expect(store.currentRepo).toMatchObject({
      id: '/workspace/repo-app',
      path: '/workspace/repo-app',
      currentBranch: 'main',
      headHash: 'f4a9c21',
      commits: [],
      changes: [],
      hasMore: true,
    })
    expect(store.isDetailsLoading('/workspace/repo-app')).toBe(false)

    // Once details resolve, the full repo replaces the shallow projection.
    setRepos([makeRepoSnapshot()])
    await store.ensureDetails('/workspace/repo-app')
    expect(store.currentRepo?.commits.length).toBeGreaterThan(0)
    expect(store.currentRepo?.branches.length).toBeGreaterThan(0)
  })

  it('surfaces initial load failures with an error and notification', async () => {
    const { toast } = await import('vue-sonner')
    getRepos.mockRejectedValue(new ApiRequestError(500, 'boom', 'error'))
    const store = useGitStore()
    await store.initialize('ws-1')

    expect(store.repos).toEqual([])
    expect(store.error).toBe('boom')
    expect(store.loading).toBe(false)
    expect(toast.error).toHaveBeenCalled()
  })

  it('shows both staged and unstaged sides of the same path', async () => {
    const both = makeRawChange('both/sides.ts', {
      status: 'M',
      staged: true,
      staged_kind: 'M',
      unstaged: 'M',
    })
    const store = await initWith([makeRepoSnapshot({ changes: [both] })])

    expect(store.stagedChanges.map((c) => c.path)).toEqual(['both/sides.ts'])
    expect(store.unstagedChanges.map((c) => c.path)).toEqual(['both/sides.ts'])
  })

  it('treats conflicts as one unresolved row, never as committable', async () => {
    const conflict = makeRawChange('conflicted.ts', {
      status: 'U',
      staged: true,
      staged_kind: 'U',
      unstaged: 'U',
      conflict: 'UU',
    })
    const store = await initWith([makeRepoSnapshot({ changes: [conflict] })])

    expect(store.unstagedChanges.map((c) => c.path)).toEqual(['conflicted.ts'])
    expect(store.stagedChanges).toEqual([])
    expect(store.unstagedChanges[0]).toMatchObject({ status: 'U', conflict: 'UU' })
  })

  it('handles empty repos, unborn HEAD and detached HEAD', async () => {
    const store = useGitStore()
    setRepos([])
    await store.initialize('ws-1')
    expect(store.repos).toEqual([])
    expect(store.currentRepo).toBeNull()
    expect(store.tagsByHash.size).toBe(0)

    const unborn = makeRepoSnapshot({
      currentBranch: 'main',
      headHash: null,
      commits: [],
      changes: [
        makeRawChange('new-file.ts', { status: 'A', staged: true, staged_kind: 'A', unstaged: null }),
      ],
    })
    setRepos([unborn])
    await store.refresh()
    expect(store.currentRepo?.headHash).toBeNull()
    expect(store.currentRepo?.commits).toEqual([])

    const detached = makeRepoSnapshot({ currentBranch: null, headHash: 'e8b7d3a' })
    setRepos([detached])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(store.currentRepo?.currentBranch).toBeNull()
    expect(store.currentBranch).toBeNull()
  })

  it('lazily loads working diffs per side and ignores stale responses', async () => {
    const path = 'dup/file.ts'
    const change = makeRawChange(path, {
      status: 'M',
      staged: true,
      staged_kind: 'M',
      unstaged: 'M',
    })
    const store = await initWith([makeRepoSnapshot({ changes: [change] })])
    const staged = makeCommitFile(path, { status: 'M', additions: 5 })
    const unstaged = makeCommitFile(path, { status: 'M', additions: 9 })
    getDiff.mockResolvedValue({
      ok: true,
      repo_path: '/workspace/repo-app',
      diff: { staged: [staged], unstaged: [unstaged] },
    })

    await store.openDiff(path, true)
    expect(store.viewingDiffChange?.staged).toBe(true)
    expect(store.viewingDiffChange?.diff).toHaveLength(1)
    expect(store.viewingDiffEntry?.additions).toBe(5)

    await store.openDiff(path, false)
    expect(store.viewingDiffChange?.staged).toBe(false)
    expect(store.viewingDiffEntry?.additions).toBe(9)

    // Cache: no second request for the same repo.
    const calls = getDiff.mock.calls.length
    await store.openDiff(path, true)
    expect(getDiff.mock.calls.length).toBe(calls)

    // Stale responses (older workspace generation) are ignored.
    getDiff.mockClear()
    getDiff.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          setTimeout(
            () =>
              resolve({
                ok: true,
                repo_path: '/workspace/repo-app',
                diff: { staged: [staged], unstaged: [unstaged] },
              }),
            20,
          )
        }),
    )
    // New repo without cache to force a fresh load racing with reset.
    store.workingDiffCache = {}
    const pending = store.openDiff(path, true)
    store.reset()
    await pending
    expect(store.viewingDiffPath).toBeNull()
  })

  it('surfaces working-diff load errors without crashing the viewer', async () => {
    const store = await initWith()
    const path = store.currentRepo!.changes[0]!.path
    delete store.workingDiffCache[store.currentRepo!.path]
    getDiff.mockRejectedValueOnce(new ApiRequestError(500, 'diff failed', 'error'))
    await store.openDiff(path)
    expect(store.workingDiffError).toBe('diff failed')
    // Snapshot metadata is still available.
    expect(store.viewingDiffChange?.path).toBe(path)
    expect(store.viewingDiffChange?.diff).toEqual([])
  })

  it('lazily loads commit details with cache and per-commit errors', async () => {
    const store = await initWith()
    const hash = store.currentRepo!.commits[0]!.hash

    await store.toggleCommitDetails(hash)
    expect(getDetails).toHaveBeenCalledWith('ws-1', '/workspace/repo-app', hash)
    expect(store.expandedCommitDetails?.hash).toBe(hash)
    expect(store.expandedCommitDetails?.fileChanges.length).toBeGreaterThan(0)

    // Cache hit: no second request.
    getDetails.mockClear()
    await store.toggleCommitDetails(hash)
    expect(store.expandedCommitHash).toBeNull()
    await store.toggleCommitDetails(hash)
    expect(getDetails).not.toHaveBeenCalled()

    // Per-commit errors are exposed per hash.
    const bad = 'deadbee'
    getDetails.mockRejectedValueOnce(new ApiRequestError(404, 'unknown commit', 'unknown_commit'))
    await store.toggleCommitDetails(bad)
    expect(store.commitDetailsErrorFor(bad)).toBe('unknown commit')
    expect(store.expandedCommitDetails).toBeNull()

    // Parent navigation loads, too.
    getDetails.mockClear()
    await store.toggleCommitDetails('9c2f1e7')
    expect(getDetails).toHaveBeenCalledWith('ws-1', '/workspace/repo-app', '9c2f1e7')
  })

  it('sends typed payloads for staging mutations and applies snapshots', async () => {
    const store = await initWith()
    const next = makeRepoSnapshot({ changes: [] })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: next, repo_path: '/workspace/repo-app' } as never)

    await store.stage('webapp/src/lib/gitGraph.ts')
    expect(opPayload()).toMatchObject({
      operation: 'stage',
      repo_path: '/workspace/repo-app',
      paths: ['webapp/src/lib/gitGraph.ts'],
    })
    expect(store.currentRepo?.changes).toEqual([])

    await store.unstage('webapp/src/stores/git.ts')
    expect(opPayload()).toMatchObject({ operation: 'unstage', paths: ['webapp/src/stores/git.ts'] })

    await store.discard('webapp/src/lib/gitGraph.ts')
    expect(opPayload()).toMatchObject({ operation: 'discard', paths: ['webapp/src/lib/gitGraph.ts'] })

    await store.stageAll()
    expect(opPayload()).toMatchObject({ operation: 'stage' })
    await store.unstageAll()
    expect(opPayload()).toMatchObject({ operation: 'unstage' })
  })

  it('commits and pushes in order, pushing only after commit success', async () => {
    const store = await initWith()
    const committed = makeRepoSnapshot({
      headHash: 'newhash1',
      commits: [makeRawCommit('newhash1', { message: 'Add git panel', parents: ['f4a9c21'] })],
      changes: [],
    })
    const pushed = { ...committed }
    runOp
      .mockResolvedValueOnce({ ok: true, snapshot: committed, repo_path: '/workspace/repo-app' } as never)
      .mockResolvedValueOnce({ ok: true, snapshot: pushed, repo_path: '/workspace/repo-app' } as never)

    const ok = await store.commit('Add git panel', true)
    expect(ok).toBe(true)
    expect(runOp).toHaveBeenCalledTimes(2)
    expect(runOp.mock.calls[0]?.[1]).toMatchObject({ operation: 'commit', message: 'Add git panel' })
    expect(runOp.mock.calls[1]?.[1]).toMatchObject({ operation: 'push' })
    expect(store.currentRepo?.headHash).toBe('newhash1')
  })

  it('does not push after a failed commit', async () => {
    const store = await initWith()
    runOp.mockRejectedValueOnce(new ApiRequestError(400, 'Nothing to commit', 'nothing_to_commit'))
    const ok = await store.commit('Add git panel', true)
    expect(ok).toBe(false)
    expect(runOp).toHaveBeenCalledTimes(1)
  })

  it('rejects empty commit messages and missing staged changes locally', async () => {
    const { toast } = await import('vue-sonner')
    const store = await initWith([makeRepoSnapshot({ changes: [] })])
    expect(await store.commit('   ')).toBe(false)
    expect(toast.error).toHaveBeenCalled()
    vi.clearAllMocks()
    expect(await store.commit('Work')).toBe(false)
    expect(runOp).not.toHaveBeenCalled()
  })

  it('suggests publish/push/pull/sync/none from branch state, busy and detached', async () => {
    const branch = (ahead: number, behind: number, upstream: string | null = 'origin/main') =>
      makeRepoSnapshot({ branches: [{ name: 'main', tip_hash: 'f4a9c21', upstream, ahead, behind }] })

    const store = await initWith([branch(2, 0)])
    expect(store.suggestedRemoteAction).toBe('push')

    setRepos([branch(2, 3)])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(store.suggestedRemoteAction).toBe('sync')

    setRepos([branch(0, 1)])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(store.suggestedRemoteAction).toBe('pull')

    setRepos([branch(0, 0)])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(store.suggestedRemoteAction).toBe('none')

    setRepos([branch(0, 0, null)])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(store.suggestedRemoteAction).toBe('publish')

    setRepos([makeRepoSnapshot({ currentBranch: null })])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(store.suggestedRemoteAction).toBe('none')

    setRepos([branch(2, 0)])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    store.busyOperation = 'push'
    expect(store.suggestedRemoteAction).toBe('none')
    store.busyOperation = null
    expect(store.suggestedRemoteAction).toBe('push')
  })

  it('rejects push while detached and reports up-to-date branches', async () => {
    const { toast } = await import('vue-sonner')
    const detached = makeRepoSnapshot({ currentBranch: null })
    const store = await initWith([detached])
    expect(await store.push()).toBe(false)
    expect(toast.error).toHaveBeenCalled()
    expect(runOp).not.toHaveBeenCalled()

    const clean = makeRepoSnapshot({
      branches: [{ name: 'main', tip_hash: 'f4a9c21', upstream: 'origin/main', ahead: 0, behind: 0 }],
    })
    setRepos([clean])
    await store.refresh()
    await store.ensureDetails('/workspace/repo-app', { force: true })
    expect(await store.push()).toBe(true)
    expect(toast.info).toHaveBeenCalled()
    expect(runOp).not.toHaveBeenCalled()
  })

  it('applies the 409 conflict snapshot and warns instead of throwing', async () => {
    const { toast } = await import('vue-sonner')
    const store = await initWith()
    const conflicted = makeRepoSnapshot({
      mergeState: { merging: true, rebasing: false, cherry_picking: false },
      changes: [
        makeRawChange('conflicted.ts', {
          status: 'U',
          staged: true,
          staged_kind: 'U',
          unstaged: 'U',
          conflict: 'UU',
        }),
      ],
    })
    runOp.mockRejectedValueOnce(
      new ApiRequestError(409, 'Merge conflict — resolve or run merge_abort', 'conflict', {
        ok: false,
        code: 'conflict',
        message: 'Merge conflict — resolve or run merge_abort',
        snapshot: conflicted,
      }),
    )
    const ok = await store.mergeIntoCurrent('feature/git-panel')
    expect(ok).toBe(false)
    expect(opPayload()).toMatchObject({ operation: 'merge_into_current', branch: 'feature/git-panel' })
    expect(store.currentRepo?.mergeState.merging).toBe(true)
    expect(store.unstagedChanges.map((c) => c.path)).toEqual(['conflicted.ts'])
    expect(toast.warning).toHaveBeenCalled()
  })

  it('covers fetch/pull/sync/checkout/branch/merge payloads', async () => {
    const store = await initWith()
    const respond = (snapshot = makeRepoSnapshot()) =>
      runOp.mockResolvedValueOnce({ ok: true, snapshot, repo_path: '/workspace/repo-app' } as never)

    respond()
    await store.fetchRemote()
    expect(opPayload()).toMatchObject({ operation: 'fetch' })

    respond()
    await store.pull()
    expect(opPayload()).toMatchObject({ operation: 'pull' })

    respond()
    await store.sync()
    expect(opPayload()).toMatchObject({ operation: 'sync' })

    respond()
    await store.checkoutBranch('feature/git-panel')
    expect(opPayload()).toMatchObject({ operation: 'checkout_branch', branch: 'feature/git-panel' })

    respond()
    await store.checkoutCommit('3d8e5b2')
    expect(opPayload()).toMatchObject({ operation: 'checkout_commit', commit: '3d8e5b2' })

    respond()
    await store.createBranch('feature/new-ui', '3d8e5b2', true)
    expect(opPayload()).toMatchObject({
      operation: 'create_branch',
      branch: 'feature/new-ui',
      start_point: '3d8e5b2',
      checkout: true,
    })

    respond()
    await store.renameBranch('main', 'main-renamed')
    expect(opPayload()).toMatchObject({
      operation: 'rename_branch',
      new_branch: 'main-renamed',
      old_branch: 'main',
    })

    respond()
    await store.deleteBranch('feature/git-panel')
    expect(opPayload()).toMatchObject({ operation: 'delete_branch', branch: 'feature/git-panel' })

    respond()
    await store.mergeCurrentInto('fix/auth-redirect')
    expect(opPayload()).toMatchObject({ operation: 'merge_current_into', target: 'fix/auth-redirect' })

    respond()
    await store.mergeAbort()
    expect(opPayload()).toMatchObject({ operation: 'merge_abort' })
  })

  it('checks out a remote branch with remote_ref and optional local_name', async () => {
    const { toast } = await import('vue-sonner')
    const store = await initWith()
    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot({ currentBranch: 'feature/x' }),
      repo_path: '/workspace/repo-app',
    } as never)

    expect(await store.checkoutRemoteBranch('origin/feature/x')).toBe(true)
    expect(opPayload()).toEqual({
      operation: 'checkout_remote_branch',
      repo_path: '/workspace/repo-app',
      remote_ref: 'origin/feature/x',
    })
    expect(store.currentRepo?.currentBranch).toBe('feature/x')
    expect(vi.mocked(toast.success).mock.calls[vi.mocked(toast.success).mock.calls.length - 1]?.[0]).toBe('Checked out feature/x')

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot({ currentBranch: 'local' }),
      repo_path: '/workspace/repo-app',
    } as never)
    expect(await store.checkoutRemoteBranch('origin/feature/x', 'local')).toBe(true)
    expect(opPayload()).toEqual({
      operation: 'checkout_remote_branch',
      repo_path: '/workspace/repo-app',
      remote_ref: 'origin/feature/x',
      local_name: 'local',
    })
    expect(vi.mocked(toast.success).mock.calls[vi.mocked(toast.success).mock.calls.length - 1]?.[0]).toBe('Checked out local')
  })

  it('publishes branches without upstream via push -u instead of up-to-date', async () => {
    const { toast } = await import('vue-sonner')
    const unpublished = makeRepoSnapshot({
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: null, ahead: 0, behind: 0 }],
      currentBranch: 'feature/fresh',
    })
    const store = await initWith([unpublished])
    expect(store.currentBranch?.upstream).toBeNull()

    const published = makeRepoSnapshot({
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: 'origin/feature/fresh', ahead: 0, behind: 0 }],
      currentBranch: 'feature/fresh',
    })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: published, repo_path: '/workspace/repo-app' } as never)
    expect(await store.push()).toBe(true)
    expect(opPayload()).toMatchObject({ operation: 'push', set_upstream: true })
    expect(toast.success).toHaveBeenCalled()
    const calls = vi.mocked(toast.success).mock.calls
    const successTitle = calls[calls.length - 1]?.[0]
    expect(successTitle).toContain('Published feature/fresh')
    expect(store.currentBranch?.upstream).toBe('origin/feature/fresh')
  })

  it('publishes via the only remote when origin is absent', async () => {
    const { toast } = await import('vue-sonner')
    const unpublished = makeRepoSnapshot({
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: null, ahead: 0, behind: 0 }],
      currentBranch: 'feature/fresh',
      remotes: ['upstream'],
      defaultRemote: null,
    })
    const store = await initWith([unpublished])
    const published = makeRepoSnapshot({
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: 'upstream/feature/fresh', ahead: 0, behind: 0 }],
      currentBranch: 'feature/fresh',
      remotes: ['upstream'],
      defaultRemote: null,
    })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: published, repo_path: '/workspace/repo-app' } as never)
    expect(await store.push()).toBe(true)
    expect(opPayload()).toMatchObject({ operation: 'push', remote: 'upstream', set_upstream: true })
    const titles = vi.mocked(toast.success).mock.calls
    expect(titles[titles.length - 1]?.[0]).toContain('Published feature/fresh to upstream')
  })

  it('prefers origin over the first remote on first publish', async () => {
    const unpublished = makeRepoSnapshot({
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: null, ahead: 0, behind: 0 }],
      currentBranch: 'feature/fresh',
      remotes: ['upstream', 'origin'],
      defaultRemote: null,
    })
    const store = await initWith([unpublished])
    const published = makeRepoSnapshot({
      branches: [{ name: 'feature/fresh', tip_hash: 'f4a9c21', upstream: 'origin/feature/fresh', ahead: 0, behind: 0 }],
      currentBranch: 'feature/fresh',
      remotes: ['upstream', 'origin'],
      defaultRemote: null,
    })
    runOp.mockResolvedValueOnce({ ok: true, snapshot: published, repo_path: '/workspace/repo-app' } as never)
    expect(await store.push()).toBe(true)
    expect(opPayload()).toMatchObject({ operation: 'push', remote: 'origin', set_upstream: true })
  })

  it('treats non-conflict 409s as normal errors without lastConflict or snapshot', async () => {
    const { toast } = await import('vue-sonner')
    const store = await initWith()
    const beforeHash = store.currentRepo?.headHash

    const cases: [string, ApiRequestError][] = [
      ['runner offline', new ApiRequestError(409, 'Runner is offline', 'runner_offline', {
        ok: false, detail: 'Runner is offline', code: 'runner_offline',
      })],
      ['workspace busy', new ApiRequestError(409, 'Workspace is busy', 'workspace_conflict', {
        ok: false, detail: 'Workspace is busy', code: 'workspace_conflict',
      })],
      ['generic conflict without snapshot', new ApiRequestError(409, 'Conflict', 'conflict', {
        ok: false, code: 'conflict', message: 'Conflict',
      })],
    ]
    for (const [label, err] of cases) {
      runOp.mockRejectedValueOnce(err)
      expect(await store.mergeIntoCurrent('feature/git-panel'), label).toBe(false)
      expect(store.lastConflict, label).toBe(false)
      expect(store.currentRepo?.headHash, label).toBe(beforeHash)
      expect(store.currentRepo?.mergeState.merging, label).toBe(false)
    }
    expect(toast.warning).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalled()
    const lastErr = vi.mocked(toast.error).mock.calls[vi.mocked(toast.error).mock.calls.length - 1]
    expect(lastErr?.[0]).toContain('Merge failed')
    expect(JSON.stringify(lastErr?.[1])).toContain('Conflict')
  })

  it('flags lastConflict only for 409 conflicts, resetting per operation', async () => {
    const store = await initWith()
    expect(store.lastConflict).toBe(false)

    const conflicted = makeRepoSnapshot({
      mergeState: { merging: true, rebasing: false, cherry_picking: false },
    })
    runOp.mockRejectedValueOnce(
      new ApiRequestError(409, 'Merge conflict — resolve or run merge_abort', 'conflict', {
        ok: false,
        code: 'conflict',
        message: 'Merge conflict — resolve or run merge_abort',
        snapshot: conflicted,
      }),
    )
    expect(await store.mergeIntoCurrent('feature/git-panel')).toBe(false)
    expect(store.lastConflict).toBe(true)

    // A following successful operation clears the flag again.
    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot(),
      repo_path: '/workspace/repo-app',
    } as never)
    expect(await store.fetchRemote()).toBe(true)
    expect(store.lastConflict).toBe(false)

    // Generic failures never set the flag.
    runOp.mockRejectedValueOnce(new ApiRequestError(400, 'merge failed', 'merge_failed'))
    expect(await store.mergeIntoCurrent('feature/git-panel')).toBe(false)
    expect(store.lastConflict).toBe(false)
  })

  it('serializes concurrent operations in call order', async () => {
    const store = await initWith()
    const order: string[] = []
    runOp.mockImplementation(
      (_ws, payload) =>
        new Promise((resolve) => {
          setTimeout(() => {
            order.push(String(payload.operation))
            resolve({ ok: true, snapshot: makeRepoSnapshot(), repo_path: '/workspace/repo-app' } as never)
          }, 10)
        }),
    )
    await Promise.all([store.stage('a.ts'), store.unstage('b.ts'), store.fetchRemote()])
    expect(order).toEqual(['stage', 'unstage', 'fetch'])
    expect(store.busyOperation).toBeNull()
  })

  it('drops stale results after workspace switches and resets cleanly', async () => {
    const store = useGitStore()
    getRepos.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          setTimeout(() => resolve({ ok: true, repos: [makeRepoSummary()] }), 20)
        }),
    )
    const first = store.initialize('ws-1')
    await store.initialize('ws-2')
    await first
    expect(store.workspaceId).toBe('ws-2')

    store.reset()
    expect(store.workspaceId).toBeNull()
    expect(store.repos).toEqual([])
    expect(store.repoDetails).toEqual({})
    expect(store.viewingDiffPath).toBeNull()
    expect(store.expandedCommitHash).toBeNull()
  })

  it('polls summaries without overlapping or toasting, keeping details intact', async () => {
    const { toast } = await import('vue-sonner')
    vi.useFakeTimers()
    try {
      const store = await initWith()
      const commitsBefore = store.currentRepo!.commits.length
      expect(commitsBefore).toBeGreaterThan(0)
      vi.clearAllMocks()
      getRepos.mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, repos: [makeRepoSummary()] }), 50)),
      )
      // Details timer parked far away so only summaries tick here.
      store.startPolling(1000, 60_000)
      store.startPolling(1000, 60_000)
      await vi.advanceTimersByTimeAsync(1050)
      // First silent tick issued exactly one request despite double start
      // (advanceTimers resolves the pending 50ms mock, then the tick fires).
      expect(getRepos.mock.calls.length).toBeLessThanOrEqual(2)
      const afterFirst = getRepos.mock.calls.length
      await vi.advanceTimersByTimeAsync(1000)
      // In-flight requests are not overlapped: at most one new request.
      expect(getRepos.mock.calls.length - afterFirst).toBeLessThanOrEqual(1)
      // Summaries-only polling never touches details or the history endpoint.
      expect(getRepo).not.toHaveBeenCalled()
      expect(getHistory).not.toHaveBeenCalled()
      expect(store.currentRepo!.commits.length).toBe(commitsBefore)
      // Drain any pending request, then verify steady polling continues.
      await vi.advanceTimersByTimeAsync(2000)
      expect(getRepos.mock.calls.length).toBeGreaterThan(afterFirst)
      expect(toast.error).not.toHaveBeenCalled()

      getRepos.mockRejectedValue(new ApiRequestError(500, 'poll failed', 'error'))
      await vi.advanceTimersByTimeAsync(2000)
      // Silent polling keeps existing summaries visible: no global error, no toast.
      expect(store.error).toBeNull()
      expect(toast.error).not.toHaveBeenCalled()

      // ... but with no summaries, silent failures surface as a global error.
      store.repos = []
      getRepos.mockRejectedValue(new ApiRequestError(500, 'poll failed', 'error'))
      await vi.advanceTimersByTimeAsync(2000)
      expect(store.error).toBe('poll failed')
      expect(toast.error).not.toHaveBeenCalled()

      store.stopPolling()
      getRepos.mockClear()
      await vi.advanceTimersByTimeAsync(5000)
      expect(getRepos).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('polls selected details on the slow timer, skipping while busy', async () => {
    vi.useFakeTimers()
    try {
      const store = await initWith()
      vi.clearAllMocks()
      getRepos.mockResolvedValue({ ok: true, repos: [makeRepoSummary()] })
      const updated = makeRepoSnapshot({ headHash: 'newhead1' })
      getRepo.mockResolvedValue({ ok: true, snapshot: updated })

      // Summaries timer parked far away; details tick every second.
      store.startPolling(60_000, 1000)
      await vi.advanceTimersByTimeAsync(1100)
      expect(getRepo).toHaveBeenCalledWith('ws-1', '/workspace/repo-app')
      expect(store.currentRepo?.headHash).toBe('newhead1')

      // Busy mutations pause the details timer.
      getRepo.mockClear()
      store.busyOperation = 'stage'
      await vi.advanceTimersByTimeAsync(3000)
      expect(getRepo).not.toHaveBeenCalled()
      store.busyOperation = null
      store.stopPolling()
    } finally {
      vi.useRealTimers()
    }
  })

  it('appends and dedupes paginated history via /history/', async () => {
    const store = await initWith([
      makeRepoSnapshot({
        hasMore: true,
        commits: [makeRawCommit('c3'), makeRawCommit('c2')],
      }),
    ])
    getHistory.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      commits: [makeRawCommit('c2'), makeRawCommit('c1')],
      has_more: false,
      history_skip: 2,
      history_limit: 50,
    })

    expect(await store.loadMoreHistory()).toBe(true)
    expect(getHistory).toHaveBeenCalledWith('ws-1', '/workspace/repo-app', { limit: 50, skip: 2 })
    expect(store.currentRepo?.commits.map((c) => c.hash)).toEqual(['c3', 'c2', 'c1'])
    expect(store.currentRepo?.hasMore).toBe(false)
    expect(await store.loadMoreHistory()).toBe(false)
  })

  it('forwards the branch filter to /history/ when paging', async () => {
    const store = await initWith([
      makeRepoSnapshot({
        hasMore: true,
        commits: [makeRawCommit('c2')],
      }),
    ])
    getHistory.mockResolvedValueOnce({
      ok: true,
      repo_path: '/workspace/repo-app',
      commits: [makeRawCommit('c1')],
      has_more: false,
      history_skip: 1,
      history_limit: 50,
    })
    expect(await store.loadMoreHistory('feature/git-panel')).toBe(true)
    expect(getHistory).toHaveBeenCalledWith('ws-1', '/workspace/repo-app', {
      limit: 50,
      skip: 1,
      branch: 'feature/git-panel',
    })
  })

  it('closes diff/commit selections gracefully on repo switches and lazy-loads details', async () => {
    const second = makeRepoSnapshot({ id: '/workspace/other', path: '/workspace/other', name: 'other' })
    const store = await initWith([makeRepoSnapshot(), second])
    const path = store.currentRepo!.changes[0]!.path
    await store.openDiff(path)
    expect(store.viewingDiffChange).not.toBeNull()

    store.selectRepo('/workspace/other')
    expect(store.currentRepo?.id).toBe('/workspace/other')
    expect(store.viewingDiffChange).toBeNull()
    // Fire-and-forget details for the new selection resolve via getGitRepo.
    await vi.waitFor(() => {
      expect(getRepo).toHaveBeenCalledWith('ws-1', '/workspace/other')
    })
    await vi.waitFor(() => {
      expect(store.repoDetails['/workspace/other']?.changes.length).toBeGreaterThan(0)
    })

    store.selectRepo('does-not-exist')
    expect(store.currentRepo?.id).toBe('/workspace/other')
  })

  it('exposes ref tags keyed by commit hash', async () => {
    const store = await initWith()
    const mainTags = store.tagsByHash.get('f4a9c21') ?? []
    expect(mainTags).toContainEqual({ name: 'main', remote: false, current: true })
    const originTags = store.tagsByHash.get('9c2f1e7') ?? []
    expect(originTags).toContainEqual({ name: 'origin/main', remote: true, current: false })
  })

  it('restores the last selected repo per workspace', async () => {
    const second = makeRepoSnapshot({
      id: '/workspace/docs',
      path: '/workspace/docs',
      name: 'docs',
      headHash: 'd1o2c3s',
    })
    const store = await initWith([makeRepoSnapshot(), second])
    expect(store.selectedRepoId).toBe('/workspace/repo-app')

    store.selectRepo('/workspace/docs')
    await flushDetails()
    expect(store.selectedRepoId).toBe('/workspace/docs')
    expect(localStorage.getItem(selectedRepoStorageKey('ws-1'))).toBe('/workspace/docs')

    // Simulate a tab reload in the same workspace: fresh store instance,
    // persisted id is restored.
    setActivePinia(createPinia())
    const reloaded = useGitStore()
    await reloaded.initialize('ws-1')
    expect(reloaded.selectedRepoId).toBe('/workspace/docs')

    // A different workspace has its own key — falls back to the first repo.
    setActivePinia(createPinia())
    const other = useGitStore()
    await other.initialize('ws-2')
    expect(other.selectedRepoId).toBe('/workspace/repo-app')
  })

  it('falls back to the first repo when the persisted repo is gone', async () => {
    localStorage.setItem(selectedRepoStorageKey('ws-1'), '/workspace/deleted')
    const store = await initWith()
    expect(store.selectedRepoId).toBe('/workspace/repo-app')
  })

  it('silent auto-fetch skips the error toast on failure', async () => {
    const { toast } = await import('vue-sonner')
    const store = await initWith()
    runOp.mockRejectedValueOnce(new ApiRequestError(500, 'fetch failed', 'fetch_failed'))
    const ok = await store.fetchRemote({ silent: true })
    expect(ok).toBe(false)
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('skips fetch while a mutation is running', async () => {
    const store = await initWith()
    store.busyOperation = 'stage'
    expect(await store.fetchRemote({ silent: true })).toBe(false)
    expect(runOp).not.toHaveBeenCalled()
    store.busyOperation = null
  })

  it('skips fetch when the repo has no remotes', async () => {
    const store = await initWith([makeRepoSnapshot({ remotes: [], defaultRemote: null })])
    expect(await store.fetchRemote({ silent: true })).toBe(false)
    expect(runOp).not.toHaveBeenCalled()
  })

  it('auto-fetches after selectRepo once details land', async () => {
    const second = makeRepoSnapshot({
      id: '/workspace/docs',
      path: '/workspace/docs',
      name: 'docs',
      headHash: 'd1o2c3s',
    })
    const store = await initWith([makeRepoSnapshot(), second])
    runOp.mockClear()
    store.selectRepo('/workspace/docs')
    await vi.waitFor(() => {
      expect(runOp).toHaveBeenCalled()
    })
    expect(runOp.mock.calls[runOp.mock.calls.length - 1]?.[1]).toMatchObject({
      operation: 'fetch',
      repo_path: '/workspace/docs',
    })
  })

  it('clamps and persists the commit details height', async () => {
    localStorage.clear()
    const store = await initWith()
    expect(store.cdvHeight).toBe(250)

    store.setCdvHeight(400)
    expect(store.cdvHeight).toBe(400)
    expect(localStorage.getItem('opencuria:git:cdvHeight')).toBe('400')

    store.setCdvHeight(50)
    expect(store.cdvHeight).toBe(120)
    store.setCdvHeight(900)
    expect(store.cdvHeight).toBe(600)
  })
})
