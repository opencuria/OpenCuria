import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import * as gitApi from '@/services/git.api'
import { useGitStore, selectedRepoStorageKey } from './git'
import { useEditorStore } from './editor'
import {
  makeCommitDetails,
  makeRawChange,
  makeRepoSnapshot,
  setupGitRepos,
} from './git.fixtures'
import type { RawGitRepoSnapshot } from '@opencuria/chat-core/gitTypes'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

vi.mock('@/services/git.api', () => ({
  generateGitCommitMessage: vi.fn(),
  getGitCommitDetails: vi.fn(),
  getGitHistory: vi.fn(),
  getGitRepo: vi.fn(),
  getGitRepos: vi.fn(),
  runGitOperation: vi.fn(),
}))

const getRepos = vi.mocked(gitApi.getGitRepos)
const getRepo = vi.mocked(gitApi.getGitRepo)
const getDetails = vi.mocked(gitApi.getGitCommitDetails)
const runOp = vi.mocked(gitApi.runGitOperation)

function setRepos(repos: RawGitRepoSnapshot[]): void {
  setupGitRepos(getRepos, getRepo, vi.mocked(gitApi.getGitHistory), repos)
}

async function bindWith(repos = [makeRepoSnapshot()], workspaceId = 'ws-1') {
  setRepos(repos)
  const store = useGitStore()
  store.bind(workspaceId, false)
  await store.refresh()
  return store
}

const docs = makeRepoSnapshot({ id: '/workspace/docs', path: '/workspace/docs', name: 'docs' })

describe('git store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
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

  it('mirrors the controller snapshot of the bound workspace', async () => {
    const store = await bindWith([makeRepoSnapshot(), docs])
    expect(getRepos).toHaveBeenCalledWith('ws-1')
    expect(store.repos).toHaveLength(2)
    expect(store.currentRepo?.id).toBe('/workspace/repo-app')
    expect(store.currentBranch).toMatchObject({ name: 'main', upstream: 'origin/main' })
    expect(store.stagedChanges.map((c) => c.path)).toEqual(['webapp/src/stores/git.ts'])
    expect(store.refGroupsFor('f4a9c21')[0]?.local).toMatchObject({ name: 'main', current: true })
  })

  it('splits both sides of a path and keeps conflicts uncommittable', async () => {
    const store = await bindWith([makeRepoSnapshot({
      changes: [
        makeRawChange('both.ts', { staged: true, staged_kind: 'M', unstaged: 'M' }),
        makeRawChange('conflict.ts', { status: 'U', staged: true, staged_kind: 'U', unstaged: 'U', conflict: 'UU' }),
      ],
    })])
    expect(store.stagedChanges.map((c) => c.path)).toEqual(['both.ts'])
    expect(store.unstagedChanges.map((c) => c.path)).toEqual(['both.ts', 'conflict.ts'])
    expect(store.bulkUnstagedPaths).toEqual(['both.ts'])
  })

  it.each([
    [2, 0, 'origin/main', 'push'],
    [2, 3, 'origin/main', 'sync'],
    [0, 1, 'origin/main', 'pull'],
    [0, 0, 'origin/main', 'none'],
    [0, 0, null, 'publish'],
  ] as const)('suggests the remote action for ahead %i behind %i upstream %s', async (
    ahead, behind, upstream, action,
  ) => {
    const store = await bindWith([makeRepoSnapshot({
      branches: [{ name: 'main', tip_hash: 'f4a9c21', upstream, ahead, behind }],
    })])
    expect(store.suggestedRemoteAction).toBe(action)
  })

  it('routes controller notices to toasts', async () => {
    const { toast } = await import('vue-sonner')
    const store = await bindWith([makeRepoSnapshot({ currentBranch: null })])
    expect(await store.push()).toBe(false)
    expect(toast.error).toHaveBeenCalled()
    expect(runOp).not.toHaveBeenCalled()
  })

  it.each([true, false])('opens the selected diff side in the editor (staged=%s)', async (staged) => {
    const path = 'renamed.ts'
    const store = await bindWith([makeRepoSnapshot({
      changes: [makeRawChange(path, { old_path: 'original.ts', staged_kind: 'R', unstaged: 'M' })],
    })])
    const openDiff = vi.spyOn(useEditorStore(), 'openDiff').mockResolvedValue()

    await store.openDiff(path, staged)

    expect(openDiff).toHaveBeenCalledExactlyOnceWith('ws-1', '/workspace/repo-app', path, staged, 'original.ts')
    expect(store.viewingDiffPath).toBe(path)
    expect(store.viewingDiffStaged).toBe(staged)
  })

  it('keeps the diff on an existing side after snapshots and closes it on repo switches', async () => {
    const both = makeRawChange('both.ts', { staged: true, staged_kind: 'M', unstaged: 'M' })
    const store = await bindWith([makeRepoSnapshot({ changes: [both] }), docs])
    vi.spyOn(useEditorStore(), 'openDiff').mockResolvedValue()
    await store.openDiff('both.ts', true)

    runOp.mockResolvedValueOnce({
      ok: true,
      snapshot: makeRepoSnapshot({ changes: [makeRawChange('both.ts', { unstaged: 'M' })] }),
    } as never)
    await store.unstage('both.ts')
    expect(store.viewingDiffStaged).toBe(false)

    store.selectRepo('/workspace/docs')
    expect(store.viewingDiffPath).toBeNull()
    await vi.waitFor(() => expect(getRepo).toHaveBeenCalledWith('ws-1', '/workspace/docs'))
  })

  it('selects commit files for the editor and clears them with the commit', async () => {
    const store = await bindWith()
    const openCommitDiff = vi.spyOn(useEditorStore(), 'openCommitDiff').mockResolvedValue()
    const hash = store.currentRepo!.commits[0]!.hash
    await store.toggleCommitDetails(hash)
    expect(store.expandedCommitDetails?.hash).toBe(hash)
    expect(store.isCommitDetailsLoading(hash)).toBe(false)

    const file = store.expandedCommitDetails!.fileChanges[0]!
    store.selectCommitFile(file.newPath)
    expect(store.viewingCommitDiff).toMatchObject({ hash, file: { newPath: file.newPath } })
    expect(openCommitDiff).toHaveBeenCalledWith('ws-1', '/workspace/repo-app', file.newPath, hash, file.oldPath)

    await store.toggleCommitDetails(hash)
    expect(store.expandedCommitHash).toBeNull()
    expect(store.viewingCommitDiff).toBeNull()

    getDetails.mockRejectedValueOnce(new Error('unknown commit'))
    await store.toggleCommitDetails('deadbee')
    expect(store.commitDetailsErrorFor('deadbee')).toBe('unknown commit')
  })

  it('remembers the selected repo path per workspace', async () => {
    const store = await bindWith([makeRepoSnapshot(), docs])
    store.selectRepo('/workspace/docs')
    expect(localStorage.getItem(selectedRepoStorageKey('ws-1'))).toBe('/workspace/docs')

    setActivePinia(createPinia())
    expect((await bindWith([makeRepoSnapshot(), docs])).selectedRepoId).toBe('/workspace/docs')
    setActivePinia(createPinia())
    expect((await bindWith([makeRepoSnapshot(), docs], 'ws-2')).selectedRepoId).toBe('/workspace/repo-app')
  })

  it('drops stale results on workspace switches and resets cleanly', async () => {
    const store = useGitStore()
    let resolve!: (value: Awaited<ReturnType<typeof gitApi.getGitRepos>>) => void
    getRepos.mockReturnValueOnce(new Promise((r) => (resolve = r)))
    store.bind('ws-1', true)
    setRepos([docs])
    store.bind('ws-2', false)
    await store.refresh()
    resolve({ ok: true, repos: [] })
    await Promise.resolve()
    expect(store.workspaceId).toBe('ws-2')
    expect(store.currentRepo?.path).toBe('/workspace/docs')

    store.reset()
    expect(store.workspaceId).toBeNull()
    expect(store.repos).toEqual([])
    expect(store.viewingDiffPath).toBeNull()
    expect(await store.stage('a.ts')).toBe(false)
    expect(await store.refresh()).toBe(false)
  })

  it('clamps and persists the commit details height', () => {
    const store = useGitStore()
    expect(store.cdvHeight).toBe(250)
    store.setCdvHeight(400)
    expect(localStorage.getItem('opencuria:git:cdvHeight')).toBe('400')
    store.setCdvHeight(50)
    expect(store.cdvHeight).toBe(120)
    store.setCdvHeight(900)
    expect(store.cdvHeight).toBe(600)
  })
})
