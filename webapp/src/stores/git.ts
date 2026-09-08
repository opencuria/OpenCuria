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
import type {
  GitBranch,
  GitCommit,
  GitCommitDetails,
  GitCommitFile,
  GitRepo,
} from '@/types/git'
import { createMockCommitDetails, createMockRepos, getMockCommitDetails } from '@/mock/git'
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

const CDV_HEIGHT_KEY = 'opencuria:git:cdvHeight'
const CDV_HEIGHT_DEFAULT = 250
const CDV_HEIGHT_MIN = 120
const CDV_HEIGHT_MAX = 600

function loadCdvHeight(): number {
  try {
    const raw = localStorage.getItem(CDV_HEIGHT_KEY)
    const parsed = raw === null ? NaN : Number.parseInt(raw, 10)
    if (!Number.isFinite(parsed)) return CDV_HEIGHT_DEFAULT
    return Math.min(CDV_HEIGHT_MAX, Math.max(CDV_HEIGHT_MIN, parsed))
  } catch {
    return CDV_HEIGHT_DEFAULT
  }
}

function persist(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Storage may be unavailable (private mode, SSR) — UI state still works.
  }
}

export const useGitStore = defineStore('git', () => {
  const notifications = useNotificationStore()

  // -- state ----------------------------------------------------------------

  const repos = ref<GitRepo[]>(createMockRepos())
  const selectedRepoId = ref<string>(repos.value[0]?.id ?? '')
  /** Path of the change whose diff is open in the main area, if any. */
  const viewingDiffPath = ref<string | null>(null)
  // -- commit details (expandable commit rows) --------------------------------
  /** Hash of the commit whose details row is expanded, if any. */
  const expandedCommitHash = ref<string | null>(null)
  /** newPath (or oldPath) of the commit file selected for diff, if any. */
  const expandedFilePath = ref<string | null>(null)
  /** Details cache keyed by commit hash. */
  const commitDetailsByHash = ref<Record<string, GitCommitDetails>>({})
  /** Commit details view height in px (persisted). */
  const cdvHeight = ref<number>(loadCdvHeight())

  // Seed the details cache with deterministic mock details.
  for (const repo of repos.value) {
    for (const [hash, details] of createMockCommitDetails(repo.commits)) {
      commitDetailsByHash.value[hash] = details
    }
  }

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

  /** Details of the expanded commit, from cache with a commit-list fallback. */
  const expandedCommitDetails = computed<GitCommitDetails | null>(() => {
    const hash = expandedCommitHash.value
    if (!hash) return null
    const cached = commitDetailsByHash.value[hash]
    if (cached) return cached
    const commit = currentRepo.value?.commits.find((c) => c.hash === hash)
    if (!commit) return null
    const fallback = getMockCommitDetails(hash)
    if (fallback) {
      commitDetailsByHash.value[hash] = fallback
      return fallback
    }
    // Last resort: synthesize empty details so expand never crashes.
    const empty: GitCommitDetails = {
      hash: commit.hash,
      parents: [...commit.parents],
      author: commit.author,
      authorEmail: '',
      authorDate: commit.timestamp,
      committer: commit.author,
      committerEmail: '',
      committerDate: commit.timestamp,
      body: commit.body ?? '',
      fileChanges: [],
    }
    commitDetailsByHash.value[hash] = empty
    return empty
  })

  /** File of the expanded commit selected for diff, if any. */
  const viewingCommitFile = computed<GitCommitFile | null>(() => {
    if (!expandedFilePath.value) return null
    const details = expandedCommitDetails.value
    if (!details) return null
    return (
      details.fileChanges.find(
        (f) =>
          f.newPath === expandedFilePath.value ||
          f.oldPath === expandedFilePath.value,
      ) ?? null
    )
  })

  /** Commit diff selection for the main area (hash + file), if any. */
  const viewingCommitDiff = computed<{ hash: string; file: GitCommitFile } | null>(
    () => {
      const details = expandedCommitDetails.value
      const file = viewingCommitFile.value
      if (!details || !file) return null
      return { hash: details.hash, file }
    },
  )

  /** Whether the given commit row is currently expanded. */
  function isCommitExpanded(hash: string): boolean {
    return expandedCommitHash.value === hash
  }

  // -- helpers ---------------------------------------------------------------

  function requireRepo(): GitRepo | null {
    const repo = currentRepo.value
    if (!repo) notifications.error('No repository selected')
    return repo
  }

  function registerEmptyDetails(commit: GitCommit): void {
    commitDetailsByHash.value[commit.hash] = {
      hash: commit.hash,
      parents: [...commit.parents],
      author: commit.author,
      authorEmail: '',
      authorDate: commit.timestamp,
      committer: commit.author,
      committerEmail: '',
      committerDate: commit.timestamp,
      body: commit.body ?? '',
      fileChanges: [],
    }
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
    // New commits carry no per-file details yet — register an empty entry
    // so expanding them never crashes.
    registerEmptyDetails(commit)
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
    closeCommitDetails()
  }

  function openDiff(path: string): void {
    // Working-tree and commit diffs are mutually exclusive.
    expandedFilePath.value = null
    viewingDiffPath.value = path
  }

  function closeDiff(): void {
    viewingDiffPath.value = null
  }

  // -- actions: commit details (expandable rows) --------------------------------

  function ensureDetails(hash: string): void {
    if (commitDetailsByHash.value[hash]) return
    const details = getMockCommitDetails(hash)
    if (details) {
      commitDetailsByHash.value[hash] = details
      return
    }
    const commit = currentRepo.value?.commits.find((c) => c.hash === hash)
    if (commit) registerEmptyDetails(commit)
  }

  function toggleCommitDetails(hash: string): void {
    if (expandedCommitHash.value === hash) {
      closeCommitDetails()
      return
    }
    expandedCommitHash.value = hash
    // Switching commits clears the previously selected file.
    expandedFilePath.value = null
    ensureDetails(hash)
  }

  function closeCommitDetails(): void {
    expandedCommitHash.value = null
    expandedFilePath.value = null
  }

  function selectCommitFile(path: string | null): void {
    // Commit and working-tree diffs are mutually exclusive.
    viewingDiffPath.value = null
    expandedFilePath.value = path
  }

  function setCdvHeight(h: number): void {
    const clamped = Math.min(CDV_HEIGHT_MAX, Math.max(CDV_HEIGHT_MIN, h))
    cdvHeight.value = clamped
    persist(CDV_HEIGHT_KEY, String(clamped))
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
    expandedCommitHash,
    expandedFilePath,
    commitDetailsByHash,
    cdvHeight,
    // getters
    currentRepo,
    currentBranch,
    stagedChanges,
    unstagedChanges,
    tagsByHash,
    viewingDiffChange,
    expandedCommitDetails,
    viewingCommitFile,
    viewingCommitDiff,
    // actions
    selectRepo,
    openDiff,
    closeDiff,
    toggleCommitDetails,
    closeCommitDetails,
    selectCommitFile,
    setCdvHeight,
    isCommitExpanded,
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
