/**
 * Git store — state and actions for the side-panel Git tab.
 *
 * Frontend-only: all actions mutate local mock data (src/mock/git.ts) so
 * the UI is fully interactive until a backend git API is wired up. Action
 * signatures are shaped like the future API calls to keep the migration
 * straightforward.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { GitBranch, GitCommit, GitRepo } from '@/types/git'
import { createMockRepos } from '@/mock/git'
import { computeGraphLayout } from '@/lib/gitGraph'
import { useNotificationStore } from '@/stores/notifications'

let commitCounter = 0

/** Generate a plausible-looking unique short hash for mock commits. */
function nextCommitHash(): string {
  commitCounter += 1
  return (0xabc0000 + commitCounter).toString(16)
}

export interface GitRefTag {
  name: string
  remote: boolean
  current: boolean
}

export const useGitStore = defineStore('git', () => {
  const notifications = useNotificationStore()

  // -- state ----------------------------------------------------------------

  const repos = ref<GitRepo[]>(createMockRepos())
  const selectedRepoId = ref<string>(repos.value[0]?.id ?? '')
  /** Path of the change whose diff is open in the main area, if any. */
  const viewingDiffPath = ref<string | null>(null)

  // -- getters --------------------------------------------------------------

  const currentRepo = computed<GitRepo | null>(
    () => repos.value.find((r) => r.id === selectedRepoId.value) ?? null,
  )

  const currentBranch = computed<GitBranch | null>(() => {
    const repo = currentRepo.value
    if (!repo || repo.currentBranch === null) return null
    return repo.branches.find((b) => b.name === repo.currentBranch) ?? null
  })

  const stagedChanges = computed(
    () => currentRepo.value?.changes.filter((c) => c.staged) ?? [],
  )
  const unstagedChanges = computed(
    () => currentRepo.value?.changes.filter((c) => !c.staged) ?? [],
  )

  const graphLayout = computed(() =>
    computeGraphLayout(currentRepo.value?.commits ?? []),
  )

  /** Branch / remote-ref tags keyed by the commit hash they point at. */
  const tagsByHash = computed<Map<string, GitRefTag[]>>(() => {
    const map = new Map<string, GitRefTag[]>()
    const repo = currentRepo.value
    if (!repo) return map
    const add = (hash: string, tag: GitRefTag): void => {
      const list = map.get(hash) ?? []
      list.push(tag)
      map.set(hash, list)
    }
    for (const branch of repo.branches) {
      add(branch.tipHash, {
        name: branch.name,
        remote: false,
        current: branch.name === repo.currentBranch,
      })
    }
    for (const ref of repo.remoteRefs) {
      add(ref.tipHash, { name: ref.name, remote: true, current: false })
    }
    return map
  })

  const viewingDiffChange = computed(() => {
    if (!viewingDiffPath.value) return null
    return (
      currentRepo.value?.changes.find((c) => c.path === viewingDiffPath.value) ??
      null
    )
  })

  // -- helpers ---------------------------------------------------------------

  function requireRepo(): GitRepo | null {
    const repo = currentRepo.value
    if (!repo) notifications.error('No repository selected')
    return repo
  }

  function appendCommit(
    repo: GitRepo,
    message: string,
    parents: string[],
  ): GitCommit {
    const commit: GitCommit = {
      hash: nextCommitHash(),
      message,
      author: 'You',
      timestamp: new Date().toISOString(),
      parents,
    }
    repo.commits.unshift(commit)
    return commit
  }

  function upsertRemoteRef(repo: GitRepo, branch: string, tipHash: string): void {
    const name = `origin/${branch}`
    const existing = repo.remoteRefs.find((r) => r.name === name)
    if (existing) {
      existing.tipHash = tipHash
    } else {
      repo.remoteRefs.push({ name, tipHash })
    }
  }

  // -- actions: selection & diff view -----------------------------------------

  function selectRepo(id: string): void {
    if (!repos.value.some((r) => r.id === id)) return
    selectedRepoId.value = id
    closeDiff()
  }

  function openDiff(path: string): void {
    viewingDiffPath.value = path
  }

  function closeDiff(): void {
    viewingDiffPath.value = null
  }

  // -- actions: staging --------------------------------------------------------

  function stage(path: string): void {
    const change = currentRepo.value?.changes.find((c) => c.path === path)
    if (change) change.staged = true
  }

  function unstage(path: string): void {
    const change = currentRepo.value?.changes.find((c) => c.path === path)
    if (change) change.staged = false
  }

  function stageAll(): void {
    currentRepo.value?.changes.forEach((c) => (c.staged = true))
  }

  function unstageAll(): void {
    currentRepo.value?.changes.forEach((c) => (c.staged = false))
  }

  function discard(path: string): void {
    const repo = requireRepo()
    if (!repo) return
    repo.changes = repo.changes.filter((c) => c.path !== path)
    if (viewingDiffPath.value === path) closeDiff()
    notifications.info('Discarded changes', path)
  }

  // -- actions: commit & push --------------------------------------------------

  function commit(message: string, andPush = false): boolean {
    const repo = requireRepo()
    if (!repo) return false
    const trimmed = message.trim()
    if (!trimmed) {
      notifications.error('Commit message is empty')
      return false
    }
    const staged = repo.changes.filter((c) => c.staged)
    if (staged.length === 0) {
      notifications.error('No staged changes to commit')
      return false
    }

    const created = appendCommit(repo, trimmed, [repo.headHash])
    repo.headHash = created.hash
    const branch = currentBranch.value
    if (branch) {
      branch.tipHash = created.hash
      branch.ahead += 1
    }
    repo.changes = repo.changes.filter((c) => !c.staged)
    notifications.success(
      `Committed on ${repo.currentBranch ?? 'detached HEAD'}`,
      created.hash,
    )
    if (andPush) push()
    return true
  }

  function push(): void {
    const repo = requireRepo()
    if (!repo) return
    const branch = currentBranch.value
    if (!branch) {
      notifications.error('Cannot push while HEAD is detached')
      return
    }
    if (branch.ahead === 0) {
      notifications.info('Everything up to date')
      return
    }
    branch.ahead = 0
    upsertRemoteRef(repo, branch.name, branch.tipHash)
    notifications.success(`Pushed to origin/${branch.name}`)
  }

  /** Mock fetch — the data never changes, so this only reports status. */
  function fetchRemote(): void {
    notifications.info('Fetched origin', 'Already up to date')
  }

  // -- actions: branches -------------------------------------------------------

  function checkoutBranch(name: string): void {
    const repo = requireRepo()
    if (!repo) return
    const branch = repo.branches.find((b) => b.name === name)
    if (!branch) return
    repo.currentBranch = name
    repo.headHash = branch.tipHash
    notifications.success(`Checked out ${name}`)
  }

  function checkoutCommit(hash: string): void {
    const repo = requireRepo()
    if (!repo || !repo.commits.some((c) => c.hash === hash)) return
    repo.currentBranch = null
    repo.headHash = hash
    notifications.info('Detached HEAD', `Checked out commit ${hash}`)
  }

  function createBranch(name: string, fromHash: string, checkout: boolean): boolean {
    const repo = requireRepo()
    if (!repo) return false
    const trimmed = name.trim()
    if (!trimmed) {
      notifications.error('Branch name is empty')
      return false
    }
    if (repo.branches.some((b) => b.name === trimmed)) {
      notifications.error(`Branch '${trimmed}' already exists`)
      return false
    }
    repo.branches.push({ name: trimmed, tipHash: fromHash, ahead: 0, behind: 0 })
    if (checkout) {
      checkoutBranch(trimmed)
    } else {
      notifications.success(`Created branch ${trimmed}`)
    }
    return true
  }

  function renameBranch(oldName: string, newName: string): boolean {
    const repo = requireRepo()
    if (!repo) return false
    const trimmed = newName.trim()
    if (!trimmed) {
      notifications.error('Branch name is empty')
      return false
    }
    if (trimmed === oldName) return true
    if (repo.branches.some((b) => b.name === trimmed)) {
      notifications.error(`Branch '${trimmed}' already exists`)
      return false
    }
    const branch = repo.branches.find((b) => b.name === oldName)
    if (!branch) return false
    branch.name = trimmed
    if (repo.currentBranch === oldName) repo.currentBranch = trimmed
    notifications.success(`Renamed branch to ${trimmed}`)
    return true
  }

  // -- actions: merging --------------------------------------------------------

  /** Merge the given branch into the currently checked out branch. */
  function mergeIntoCurrent(branchName: string): void {
    const repo = requireRepo()
    const branch = currentBranch.value
    if (!repo || !branch) return
    const source = repo.branches.find((b) => b.name === branchName)
    if (!source || source.name === branch.name) return
    const created = appendCommit(
      repo,
      `Merge branch '${source.name}' into ${branch.name}`,
      [branch.tipHash, source.tipHash],
    )
    branch.tipHash = created.hash
    branch.ahead += 1
    repo.headHash = created.hash
    notifications.success(`Merged ${source.name} into ${branch.name}`)
  }

  /** Merge the currently checked out branch into the given branch. */
  function mergeCurrentInto(branchName: string): void {
    const repo = requireRepo()
    const branch = currentBranch.value
    if (!repo || !branch) return
    const target = repo.branches.find((b) => b.name === branchName)
    if (!target || target.name === branch.name) return
    const created = appendCommit(
      repo,
      `Merge branch '${branch.name}' into ${target.name}`,
      [target.tipHash, branch.tipHash],
    )
    target.tipHash = created.hash
    target.ahead += 1
    notifications.success(`Merged ${branch.name} into ${target.name}`)
  }

  return {
    // state
    repos,
    selectedRepoId,
    viewingDiffPath,
    // getters
    currentRepo,
    currentBranch,
    stagedChanges,
    unstagedChanges,
    graphLayout,
    tagsByHash,
    viewingDiffChange,
    // actions
    selectRepo,
    openDiff,
    closeDiff,
    stage,
    unstage,
    stageAll,
    unstageAll,
    discard,
    commit,
    push,
    fetchRemote,
    checkoutBranch,
    checkoutCommit,
    createBranch,
    renameBranch,
    mergeIntoCurrent,
    mergeCurrentInto,
  }
})
