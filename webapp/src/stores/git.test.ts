import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useGitStore } from './git'
import { toast } from 'vue-sonner'

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

describe('git store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('starts with the first mock repo selected', () => {
    const store = useGitStore()

    expect(store.repos.length).toBe(3)
    expect(store.currentRepo?.id).toBe('repo-app')
    expect(store.currentBranch?.name).toBe('main')
    expect(store.stagedChanges.length).toBe(2)
    expect(store.unstagedChanges.length).toBe(3)
  })

  it('switches repositories and closes an open diff', () => {
    const store = useGitStore()
    store.openDiff(store.currentRepo!.changes[0]!.path)
    expect(store.viewingDiffChange).not.toBeNull()

    store.selectRepo('repo-docs')

    expect(store.currentRepo?.id).toBe('repo-docs')
    expect(store.viewingDiffChange).toBeNull()

    store.selectRepo('does-not-exist')
    expect(store.currentRepo?.id).toBe('repo-docs')
  })

  it('stages, unstages and discards changes', () => {
    const store = useGitStore()
    const unstaged = store.unstagedChanges[0]!

    store.stage(unstaged.path)
    expect(store.stagedChanges.some((c) => c.path === unstaged.path)).toBe(true)

    store.unstage(unstaged.path)
    expect(store.unstagedChanges.some((c) => c.path === unstaged.path)).toBe(
      true,
    )

    store.stageAll()
    expect(store.unstagedChanges.length).toBe(0)
    store.unstageAll()
    expect(store.stagedChanges.length).toBe(0)

    store.openDiff(unstaged.path)
    store.discard(unstaged.path)
    expect(
      store.currentRepo!.changes.some((c) => c.path === unstaged.path),
    ).toBe(false)
    expect(store.viewingDiffPath).toBeNull()
    expect(toast.info).toHaveBeenCalled()
  })

  it('commits staged changes onto the current branch', () => {
    const store = useGitStore()
    const repo = store.currentRepo!
    const before = repo.commits.length
    const previousTip = repo.branches.find((b) => b.name === 'main')!.tipHash
    const previousAhead = repo.branches.find((b) => b.name === 'main')!.ahead

    const ok = store.commit('Add git panel')

    expect(ok).toBe(true)
    expect(repo.commits.length).toBe(before + 1)
    const created = repo.commits[0]!
    expect(created.message).toBe('Add git panel')
    expect(created.parents).toEqual([previousTip])
    expect(repo.headHash).toBe(created.hash)
    const main = repo.branches.find((b) => b.name === 'main')!
    expect(main.tipHash).toBe(created.hash)
    expect(main.ahead).toBe(previousAhead + 1)
    expect(store.stagedChanges.length).toBe(0)
    expect(toast.success).toHaveBeenCalled()
  })

  it('rejects commits without a message or staged changes', () => {
    const store = useGitStore()

    expect(store.commit('   ')).toBe(false)
    expect(toast.error).toHaveBeenCalledWith(
      'Commit message is empty',
      expect.anything(),
    )

    store.unstageAll()
    expect(store.commit('Work')).toBe(false)
    expect(toast.error).toHaveBeenCalledWith(
      'No staged changes to commit',
      expect.anything(),
    )
  })

  it('commit & push resets ahead and updates the remote ref', () => {
    const store = useGitStore()
    const repo = store.currentRepo!

    const ok = store.commit('Add git panel', true)

    expect(ok).toBe(true)
    const main = repo.branches.find((b) => b.name === 'main')!
    expect(main.ahead).toBe(0)
    expect(
      repo.remoteRefs.find((r) => r.name === 'origin/main')?.tipHash,
    ).toBe(main.tipHash)
    expect(toast.success).toHaveBeenCalledWith(
      'Pushed to origin/main',
      expect.anything(),
    )
  })

  it('push is a no-op with a toast when everything is up to date', () => {
    const store = useGitStore()
    store.checkoutBranch('feature/git-panel')
    expect(store.currentBranch?.ahead).toBe(0)

    store.push()

    expect(toast.info).toHaveBeenCalledWith(
      'Everything up to date',
      expect.anything(),
    )
  })

  it('checks out branches and commits (detached HEAD)', () => {
    const store = useGitStore()
    const repo = store.currentRepo!

    store.checkoutBranch('feature/git-panel')
    expect(repo.currentBranch).toBe('feature/git-panel')
    expect(repo.headHash).toBe('g5h1k83')

    store.checkoutCommit('3d8e5b2')
    expect(repo.currentBranch).toBeNull()
    expect(repo.headHash).toBe('3d8e5b2')
    expect(store.currentBranch).toBeNull()

    // Push is rejected while detached
    store.push()
    expect(toast.error).toHaveBeenCalledWith(
      'Cannot push while HEAD is detached',
      expect.anything(),
    )
  })

  it('creates branches and rejects duplicates', () => {
    const store = useGitStore()
    const repo = store.currentRepo!

    expect(store.createBranch('feature/new-ui', '3d8e5b2', false)).toBe(true)
    const created = repo.branches.find((b) => b.name === 'feature/new-ui')
    expect(created?.tipHash).toBe('3d8e5b2')
    expect(repo.currentBranch).toBe('main')

    expect(store.createBranch('feature/new-ui', '3d8e5b2', false)).toBe(false)
    expect(toast.error).toHaveBeenCalledWith(
      "Branch 'feature/new-ui' already exists",
      expect.anything(),
    )
    expect(store.createBranch('  ', '3d8e5b2', false)).toBe(false)
  })

  it('creates a branch and checks it out in one step', () => {
    const store = useGitStore()
    const repo = store.currentRepo!

    expect(store.createBranch('feature/checkout-me', 'c2d3e4f', true)).toBe(true)
    expect(repo.currentBranch).toBe('feature/checkout-me')
    expect(repo.headHash).toBe('c2d3e4f')
  })

  it('renames branches, including the current one', () => {
    const store = useGitStore()
    const repo = store.currentRepo!

    expect(store.renameBranch('main', 'main-renamed')).toBe(true)
    expect(repo.branches.some((b) => b.name === 'main-renamed')).toBe(true)
    expect(repo.currentBranch).toBe('main-renamed')

    expect(store.renameBranch('fix/auth-redirect', 'main-renamed')).toBe(false)
    expect(toast.error).toHaveBeenCalledWith(
      "Branch 'main-renamed' already exists",
      expect.anything(),
    )
  })

  it('merges a branch into the current branch', () => {
    const store = useGitStore()
    const repo = store.currentRepo!
    const mainTip = repo.branches.find((b) => b.name === 'main')!.tipHash

    store.mergeIntoCurrent('feature/git-panel')

    const merge = repo.commits[0]!
    expect(merge.message).toBe("Merge branch 'feature/git-panel' into main")
    expect(merge.parents).toEqual([mainTip, 'g5h1k83'])
    const main = repo.branches.find((b) => b.name === 'main')!
    expect(main.tipHash).toBe(merge.hash)
    expect(repo.headHash).toBe(merge.hash)
  })

  it('merges the current branch into another branch', () => {
    const store = useGitStore()
    const repo = store.currentRepo!
    const mainTip = repo.branches.find((b) => b.name === 'main')!.tipHash
    const targetTip = repo.branches.find(
      (b) => b.name === 'fix/auth-redirect',
    )!.tipHash

    store.mergeCurrentInto('fix/auth-redirect')

    const merge = repo.commits[0]!
    expect(merge.message).toBe("Merge branch 'main' into fix/auth-redirect")
    expect(merge.parents).toEqual([targetTip, mainTip])
    const target = repo.branches.find((b) => b.name === 'fix/auth-redirect')!
    expect(target.tipHash).toBe(merge.hash)
    // HEAD stays on the current branch
    expect(repo.currentBranch).toBe('main')
    expect(repo.headHash).toBe(mainTip)
  })

  it('exposes ref tags keyed by commit hash', () => {
    const store = useGitStore()

    const mainTags = store.tagsByHash.get('f4a9c21') ?? []
    expect(mainTags).toContainEqual({
      name: 'main',
      remote: false,
      current: true,
    })

    const originMainTags = store.tagsByHash.get('9c2f1e7') ?? []
    expect(originMainTags).toContainEqual({
      name: 'origin/main',
      remote: true,
      current: false,
    })
  })
})
