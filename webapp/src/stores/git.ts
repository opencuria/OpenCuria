/**
 * Git store — productive backend integration for the side-panel Git tab.
 *
 * All data comes from the backend git API (`src/services/git.api.ts`):
 * light repo summaries (`GET /git/repos/`), lazy per-repo snapshots
 * (`GET /git/repo/`), paged history (`GET /git/history/`), lazy
 * working-tree diffs (`GET /git/diff/`), lazy commit details
 * (`GET /git/commits/{hash}/`) and typed mutations
 * (`POST /git/operation/`). There is no mock fallback.
 *
 * Key behaviours:
 * - `initialize(workspaceId)` binds the store to a workspace and loads
 *   summaries + details for the selected repo; `refresh({silent})`
 *   reloads summaries (plus selected details unless `withDetails: false`);
 *   `reset()` clears everything.
 * - `repos` holds light summaries only; full data lives in `repoDetails`
 *   (keyed by repo path) and is loaded lazily via `ensureDetails`.
 *   `currentRepo` falls back to a shallow projection of the summary until
 *   details arrive.
 * - `startPolling` / `stopPolling` drive two silent timers: summaries
 *   (4s) and selected-repo details (15s) — no overlap,
 *   page-visibility aware; a request generation guards against stale
 *   workspace results.
 * - Mutations are serialized through a promise chain and report through
 *   the notification store. HTTP 409 merge conflicts still carry a fresh
 *   snapshot (`ApiRequestError.data.snapshot`) which is applied.
 * - One snapshot row per path can carry BOTH a staged and an unstaged
 *   change; conflicts surface as exactly one unresolved `U` row (never as
 *   a committable staged change). Diff selection distinguishes the side
 *   via `openDiff(path, staged)`.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  GitBranch,
  GitCommit,
  GitCommitDetails,
  GitCommitFile,
  GitCommitFileStatus,
  GitFileChange,
  GitFileStatus,
  GitMergeState,
  GitRemoteRef,
  GitRepo,
  GitRepoSummary,
  GitStagedKind,
  GitUnstagedKind,
} from '@/types/git'
import { ApiRequestError } from '@/services/api'
import { groupRefTags, type GitRefGroup } from '@/lib/gitRefGroups'
import {
  conflictSnapshotOf,
  getGitCommitDetails,
  getGitHistory,
  getGitRepo,
  getGitRepos,
  getGitWorkingDiff,
  runGitOperation,
  type GitOperationRequest,
  type RawGitChange,
  type RawGitCommit,
  type RawGitCommitDetails,
  type RawGitCommitFile,
  type RawGitDiffHunk,
  type RawGitRepoSnapshot,
  type RawGitRepoSummary,
} from '@/services/git.api'
import { useNotificationStore } from '@/stores/notifications'

export type { GitRefTag } from '@/types/git'

const CDV_HEIGHT_KEY = 'opencuria:git:cdvHeight'
const CDV_HEIGHT_DEFAULT = 250
const CDV_HEIGHT_MIN = 120
const CDV_HEIGHT_MAX = 600

/** Silent summaries polling interval (ms). */
export const GIT_POLL_INTERVAL_MS = 4000
/** Silent selected-details polling interval (ms). */
export const GIT_DETAILS_POLL_MS = 15000
/** Page size for per-repo history requests. */
export const GIT_HISTORY_LIMIT = 50

/** Suggested remote action for the changes-section primary button. */
export type SuggestedRemoteAction = 'publish' | 'push' | 'pull' | 'sync' | 'none'

export function normalizeRepoSummary(raw: RawGitRepoSummary): GitRepoSummary {
  const path = raw.path
  return {
    id: raw.id || path,
    name: raw.name || path.split('/').filter(Boolean).pop() || path,
    path,
    currentBranch: raw.current_branch ?? null,
    headHash: raw.head_hash ?? null,
  }
}

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

const SELECTED_REPO_KEY_PREFIX = 'opencuria:git:selectedRepo:'

/** Storage key for the last selected repo of a workspace (per-workspace). */
export function selectedRepoStorageKey(workspaceId: string): string {
  return `${SELECTED_REPO_KEY_PREFIX}${workspaceId}`
}

function loadSelectedRepoId(wsId: string | null): string | null {
  try {
    if (!wsId) return null
    const raw = localStorage.getItem(selectedRepoStorageKey(wsId))
    return raw ? raw : null
  } catch {
    return null
  }
}

function saveSelectedRepoId(wsId: string | null, id: string): void {
  if (!wsId || !id) return
  persist(selectedRepoStorageKey(wsId), id)
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiRequestError) return e.message || 'Unknown error'
  if (e instanceof Error) return e.message || 'Unknown error'
  return typeof e === 'string' && e ? e : 'Unknown error'
}

/**
 * Module-level working-diff request sequence. Lives outside the setup
 * closure on purpose: Pinia setup stores expose state, but per-request
 * invalidation tokens must never be reset/ref-counted by state resets —
 * `closeDiff`/`reset` bump the sequence so late losers are always dropped.
 * (Kept outside `defineStore` so HMR re-execution can't resurrect a stale
 * closure counter either.)
 */
const useGitWorkingDiffSeq = (() => {
  let seq = 0
  return {
    next(): number {
      seq += 1
      return seq
    },
    invalidate(): void {
      seq += 1
    },
    isCurrent(req: number): boolean {
      return req === seq
    },
  }
})()

// ---------------------------------------------------------------------------
// Normalization (snake_case wire -> camelCase domain)
// ---------------------------------------------------------------------------

function asFileStatus(value: unknown): GitFileStatus {
  return value === 'A' ||
    value === 'D' ||
    value === 'R' ||
    value === 'C' ||
    value === 'U'
    ? value
    : 'M'
}

function asCommitFileStatus(value: unknown): GitCommitFileStatus {
  return value === 'A' ||
    value === 'M' ||
    value === 'D' ||
    value === 'R' ||
    value === 'C' ||
    value === 'U'
    ? value
    : 'M'
}

function asStagedKind(value: unknown): GitStagedKind {
  return value === 'M' ||
    value === 'A' ||
    value === 'D' ||
    value === 'R' ||
    value === 'C' ||
    value === 'U'
    ? value
    : null
}

function asUnstagedKind(value: unknown): GitUnstagedKind {
  return value === 'M' ||
    value === 'D' ||
    value === 'R' ||
    value === 'C' ||
    value === 'U' ||
    value === 'untracked'
    ? value
    : null
}

export function normalizeDiffHunk(raw: RawGitDiffHunk): GitCommitFile['diff'][number] {
  return {
    header: raw.header,
    oldStart: raw.old_start,
    newStart: raw.new_start,
    lines: (raw.lines ?? []).map((line) => ({ ...line })),
  }
}

export function normalizeCommitFile(raw: RawGitCommitFile): GitCommitFile {
  return {
    oldPath: raw.old_path,
    newPath: raw.new_path,
    status: asCommitFileStatus(raw.status),
    additions: raw.additions ?? 0,
    deletions: raw.deletions ?? 0,
    binary: raw.binary ?? false,
    truncated: raw.truncated ?? false,
    hasTextualDiff: raw.has_textual_diff ?? false,
    diff: (raw.diff ?? []).map(normalizeDiffHunk),
  }
}

export function normalizeChange(raw: RawGitChange): GitFileChange {
  return {
    path: raw.path,
    oldPath: raw.old_path ?? null,
    status: asFileStatus(raw.status),
    staged: raw.staged === true,
    stagedKind: asStagedKind(raw.staged_kind),
    unstaged: asUnstagedKind(raw.unstaged),
    conflict: typeof raw.conflict === 'string' && raw.conflict ? raw.conflict : null,
    diff: (raw.diff ?? []).map(normalizeDiffHunk),
  }
}

export function normalizeCommit(raw: RawGitCommit): GitCommit {
  return {
    hash: raw.hash,
    message: raw.message ?? '',
    body: raw.body ?? '',
    author: raw.author ?? '',
    email: raw.author_email ?? '',
    authorEmail: raw.author_email ?? '',
    timestamp: raw.timestamp || raw.author_date || '',
    authorDate: raw.author_date ?? '',
    committer: raw.committer ?? '',
    committerEmail: raw.committer_email ?? '',
    committerDate: raw.committer_date ?? '',
    parents: [...(raw.parents ?? [])],
  }
}

export function normalizeCommitDetails(raw: RawGitCommitDetails): GitCommitDetails {
  return {
    hash: raw.hash,
    message: raw.message ?? '',
    parents: [...(raw.parents ?? [])],
    author: raw.author ?? '',
    authorEmail: raw.author_email ?? '',
    authorDate: raw.author_date ?? '',
    committer: raw.committer ?? '',
    committerEmail: raw.committer_email ?? '',
    committerDate: raw.committer_date ?? '',
    body: raw.body ?? '',
    fileChanges: (raw.file_changes ?? []).map(normalizeCommitFile),
  }
}

export function normalizeRepoSnapshot(raw: RawGitRepoSnapshot): GitRepo {
  const mergeState: GitMergeState = {
    merging: raw.merge_state?.merging === true,
    rebasing: raw.merge_state?.rebasing === true,
    cherryPicking: raw.merge_state?.cherry_picking === true,
  }
  const branches: GitBranch[] = (raw.branches ?? []).map((b) => ({
    name: b.name,
    tipHash: b.tip_hash,
    upstream: b.upstream ?? null,
    ahead: b.ahead ?? 0,
    behind: b.behind ?? 0,
  }))
  const remoteRefs: GitRemoteRef[] = (raw.remote_refs ?? []).map((r) => ({
    name: r.name,
    tipHash: r.tip_hash,
  }))
  return {
    id: raw.id || raw.path,
    name: raw.name || raw.path.split('/').filter(Boolean).pop() || raw.path,
    path: raw.path,
    currentBranch: raw.current_branch ?? null,
    headHash: raw.head_hash ?? null,
    branches,
    remoteRefs,
    remotes: [...(raw.remotes ?? [])],
    defaultRemote: raw.default_remote ?? null,
    upstream: raw.upstream ?? null,
    ahead: raw.ahead ?? 0,
    behind: raw.behind ?? 0,
    mergeState,
    commits: (raw.commits ?? []).map(normalizeCommit),
    hasMore: raw.has_more === true,
    historySkip: raw.history_skip ?? 0,
    historyLimit: raw.history_limit ?? GIT_HISTORY_LIMIT,
    changes: (raw.changes ?? []).map(normalizeChange),
  }
}

/** Cache key for per-repo commit details. */
export function commitDetailsKey(repoPath: string, hash: string): string {
  return `${repoPath}::${hash}`
}

export const useGitStore = defineStore('git', () => {
  const notifications = useNotificationStore()

  // -- state ------------------------------------------------------------------

  /** Workspace this store is currently bound to (null after `reset`). */
  const workspaceId = ref<string | null>(null)
  /** Light repo summaries (no branches/commits/changes). */
  const repos = ref<GitRepoSummary[]>([])
  /** Full per-repo details keyed by repo path (lazy via ensureDetails). */
  const repoDetails = ref<Record<string, GitRepo>>({})
  /** Per-repo detail loading flags keyed by repo path (for UI spinners). */
  const detailsLoading = ref<Record<string, boolean>>({})
  const selectedRepoId = ref<string>('')
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** Label of the currently running mutation, if any. */
  const busyOperation = ref<string | null>(null)
  /** True while a paginated history page is loading. */
  const historyLoading = ref(false)
  /**
   * True when the most recent finished mutation ended in an HTTP 409 merge
   * conflict (snapshot already applied, warning already toasted).
   *
   * Lets callers distinguish "conflict — guide the user to the Changes
   * banner" from generic failures while keeping the established boolean
   * `merge...()` APIs untouched. Reset on every new operation start,
   * on success and on non-conflict errors.
   */
  const lastConflict = ref(false)

  function clearLastConflict(): void {
    lastConflict.value = false
  }

  /** Path of the working-tree change whose diff is open, if any. */
  const viewingDiffPath = ref<string | null>(null)
  /**
   * Selected diff side for `viewingDiffPath`: true = staged, false =
   * unstaged, null = legacy selection without a side (falls back to the
   * snapshot row).
   */
  const viewingDiffStaged = ref<boolean | null>(null)
  const workingDiffLoading = ref(false)
  const workingDiffError = ref<string | null>(null)
  /** Lazily loaded staged/unstaged file diffs keyed by repo path. */
  const workingDiffCache = ref<
    Record<string, { staged: GitCommitFile[]; unstaged: GitCommitFile[] }>
  >({})

  /** Hash of the commit whose details row is expanded, if any. */
  const expandedCommitHash = ref<string | null>(null)
  /** newPath (or oldPath) of the commit file selected for diff, if any. */
  const expandedFilePath = ref<string | null>(null)
  /** Commit details cache keyed by `${repoPath}::${hash}`. */
  const commitDetailsCache = ref<Record<string, GitCommitDetails>>({})
  const commitDetailsLoading = ref<Record<string, boolean>>({})
  const commitDetailsError = ref<Record<string, string | null>>({})

  /** Commit details view height in px (persisted). */
  const cdvHeight = ref<number>(loadCdvHeight())

  /**
   * Legacy projection of the details cache keyed by hash (current repo
   * wins). New code should use `commitDetailsCache` / `expandedCommitDetails`.
   */
  const commitDetailsByHash = computed<Record<string, GitCommitDetails>>(() => {
    const out: Record<string, GitCommitDetails> = {}
    const currentPath = currentRepo.value?.path
    const entries = Object.entries(commitDetailsCache.value)
    entries.sort(([a], [b]) => {
      const aCurrent = currentPath !== undefined && a.startsWith(`${currentPath}::`)
      const bCurrent = currentPath !== undefined && b.startsWith(`${currentPath}::`)
      return Number(bCurrent) - Number(aCurrent)
    })
    for (const [key, details] of entries) {
      const hash = key.slice(key.lastIndexOf('::') + 2)
      if (!(hash in out)) out[hash] = details
    }
    return out
  })

  // -- request bookkeeping (stale guards, serialization, polling) --------------

  /** Bumped on initialize/reset; async results with an older gen are dropped. */
  let loadGen = 0
  /** Workspace id the in-flight `refresh` belongs to (null when idle). */
  let refreshFor: string | null = null
  /** Generation the in-flight `refresh` belongs to (join only on match). */
  let refreshGen = -1
  /** True while the current refresh promise is still pending. */
  let refreshPromise: Promise<boolean> | null = null
  /** Serializes mutations so backend per-repo locks never interleave. */
  let opQueue: Promise<void> = Promise.resolve()
  let summaryTimer: ReturnType<typeof setInterval> | null = null
  let detailsTimer: ReturnType<typeof setInterval> | null = null
  const polling = ref(false)
  let visibilityListener: (() => void) | null = null
  /** Guards overlapping ensureDetails calls per repo path. */
  const detailsInFlight = new Set<string>()

  // -- getters ------------------------------------------------------------------

  function summaryForId(id: string): GitRepoSummary | null {
    return repos.value.find((r) => r.id === id) ?? null
  }

  function selectedSummary(): GitRepoSummary | null {
    return summaryForId(selectedRepoId.value)
  }

  /** Shallow fallback projection until details are loaded. */
  function shallowRepo(summary: GitRepoSummary): GitRepo {
    return {
      id: summary.id,
      name: summary.name,
      path: summary.path,
      currentBranch: summary.currentBranch,
      headHash: summary.headHash,
      branches: [],
      remoteRefs: [],
      remotes: [],
      defaultRemote: null,
      upstream: null,
      ahead: 0,
      behind: 0,
      mergeState: { merging: false, rebasing: false, cherryPicking: false },
      commits: [],
      hasMore: true,
      historySkip: 0,
      historyLimit: GIT_HISTORY_LIMIT,
      changes: [],
    }
  }

  const currentRepo = computed<GitRepo | null>(() => {
    const summary = summaryForId(selectedRepoId.value)
    if (!summary) return null
    return repoDetails.value[summary.path] ?? shallowRepo(summary)
  })

  const currentBranch = computed<GitBranch | null>(() => {
    const repo = currentRepo.value
    if (!repo || repo.currentBranch === null) return null
    return repo.branches.find((b) => b.name === repo.currentBranch) ?? null
  })

  /**
   * Suggested remote action for the changes-section primary button: always
   * propose whatever makes most sense right now (publish > sync > push >
   * pull, none when up to date / detached / busy).
   */
  const suggestedRemoteAction = computed<SuggestedRemoteAction>(() => {
    if (busyOperation.value !== null) return 'none'
    const repo = currentRepo.value
    if (!repo || repo.currentBranch === null) return 'none'
    const branch = currentBranch.value
    if (!branch) return 'none'
    if (branch.upstream == null) return 'publish'
    const ahead = branch.ahead ?? 0
    const behind = branch.behind ?? 0
    if (ahead > 0 && behind > 0) return 'sync'
    if (ahead > 0) return 'push'
    if (behind > 0) return 'pull'
    return 'none'
  })

  /**
   * Staged working-tree changes. Conflicted (`U`) rows are excluded — they
   * are unresolved and must never look committable.
   */
  const stagedChanges = computed(
    () =>
      currentRepo.value?.changes.filter(
        (c) => c.stagedKind !== null && c.conflict === null,
      ) ?? [],
  )

  /**
   * Unstaged working-tree changes. A path with both sides appears here AND
   * in `stagedChanges` (one row per side); conflicts appear exactly once,
   * here.
   */
  const unstagedChanges = computed(
    () => currentRepo.value?.changes.filter((c) => c.unstaged !== null) ?? [],
  )

  /** Branch / remote-ref tags keyed by the commit hash they point at. */
  const tagsByHash = computed(() => {
    const map = new Map<
      string,
      { name: string; remote: boolean; current: boolean }[]
    >()
    const repo = currentRepo.value
    if (!repo) return map
    const add = (
      hash: string,
      tag: { name: string; remote: boolean; current: boolean },
    ): void => {
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

  /**
   * Grouped local + remote ref badges keyed by commit hash (vscode-git-graph
   * `getBranchLabels` behaviour): a remote ref at the same commit joins the
   * local branch group with the same name. `tagsByHash` stays untouched.
   */
  const refGroupsByHash = computed(() => {
    const map = new Map<string, GitRefGroup[]>()
    const repo = currentRepo.value
    if (!repo) return map
    for (const [hash, tags] of tagsByHash.value) {
      map.set(hash, groupRefTags(tags, repo.remotes))
    }
    return map
  })

  /** Grouped ref badges for one commit hash (HEAD pseudo-tag still added by the caller). */
  function refGroupsFor(hash: string): GitRefGroup[] {
    return refGroupsByHash.value.get(hash) ?? []
  }

  /** Lazily loaded cache entry for the current diff selection, if any. */
  const viewingDiffEntry = computed<GitCommitFile | null>(() => {
    const path = viewingDiffPath.value
    const repo = currentRepo.value
    if (!path || !repo) return null
    const cache = workingDiffCache.value[repo.path]
    if (!cache) return null
    const stagedSide = viewingDiffStaged.value ?? repo.changes.find((c) => c.path === path)?.staged ?? true
    const list = stagedSide ? cache.staged : cache.unstaged
    return (
      list.find((f) => f.newPath === path || f.oldPath === path) ?? null
    )
  })

  /**
   * Working-tree change selected for the main-area diff: snapshot metadata
   * enriched with the lazily loaded hunks of the selected side.
   */
  const viewingDiffChange = computed<GitFileChange | null>(() => {
    const path = viewingDiffPath.value
    const repo = currentRepo.value
    if (!path || !repo) return null
    const row = repo.changes.find((c) => c.path === path) ?? null
    if (!row) return null
    const stagedSide =
      viewingDiffStaged.value ?? row.staged
    return { ...row, staged: stagedSide, diff: viewingDiffEntry.value?.diff ?? [] }
  })

  /** Details of the expanded commit (null until lazily loaded). */
  const expandedCommitDetails = computed<GitCommitDetails | null>(() => {
    const hash = expandedCommitHash.value
    const repo = currentRepo.value
    if (!hash || !repo) return null
    return commitDetailsCache.value[commitDetailsKey(repo.path, hash)] ?? null
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

  function isCommitDetailsLoading(hash: string): boolean {
    const repo = currentRepo.value
    if (!repo) return false
    return commitDetailsLoading.value[commitDetailsKey(repo.path, hash)] === true
  }

  function commitDetailsErrorFor(hash: string): string | null {
    const repo = currentRepo.value
    if (!repo) return null
    return commitDetailsError.value[commitDetailsKey(repo.path, hash)] ?? null
  }

  function isDetailsLoading(repoPath: string): boolean {
    return detailsLoading.value[repoPath] === true
  }

  // -- snapshot handling ----------------------------------------------------------

  /**
   * Replace the summary list (summaries refresh), keeping the selection.
   * Detail rows stay intact (branches/commits/changes are never
   * overwritten); only `currentBranch`/`headHash` are merged from the
   * summary so headers stay fresh while polling.
   */
  function applyRepoSummaries(next: GitRepoSummary[]): void {
    const previous = selectedSummary()
    repos.value = next
    for (const summary of next) {
      const detail = repoDetails.value[summary.path]
      if (
        detail &&
        (detail.currentBranch !== summary.currentBranch ||
          detail.headHash !== summary.headHash)
      ) {
        repoDetails.value = {
          ...repoDetails.value,
          [summary.path]: {
            ...detail,
            currentBranch: summary.currentBranch,
            headHash: summary.headHash,
          },
        }
      }
    }
    if (next.length === 0) {
      selectedRepoId.value = ''
      closeDiff()
      closeCommitDetails()
      return
    }
    // Priority 1: persisted selection (still present) — restores the last
    // repo across sessions/tab switches. Unknown IDs fall through silently.
    const persistedId = loadSelectedRepoId(workspaceId.value)
    const persisted = persistedId
      ? next.find((r) => r.id === persistedId)
      : undefined
    if (persisted) {
      if (selectedRepoId.value !== persisted.id) {
        selectedRepoId.value = persisted.id
        closeDiff()
        closeCommitDetails()
      } else {
        const detail = currentRepo.value
        if (detail) reconcileDiffSelection(detail)
      }
      return
    }
    if (next.some((r) => r.id === selectedRepoId.value)) {
      // Selection survived — drop view selections that no longer exist.
      saveSelectedRepoId(workspaceId.value, selectedRepoId.value)
      const detail = currentRepo.value
      if (detail) reconcileDiffSelection(detail)
      return
    }
    const byPath = previous ? next.find((r) => r.path === previous.path) : undefined
    selectedRepoId.value = (byPath ?? next[0])!.id
    saveSelectedRepoId(workspaceId.value, selectedRepoId.value)
    closeDiff()
    closeCommitDetails()
  }

  /**
   * Replace a single repo's details from a mutation/single snapshot,
   * keeping the selection, and sync the summary entry (branch/head).
   */
  function applyRepoSnapshot(snapshot: GitRepo): void {
    repoDetails.value = { ...repoDetails.value, [snapshot.path]: snapshot }
    const index = repos.value.findIndex(
      (r) => r.path === snapshot.path || r.id === snapshot.id,
    )
    const summary: GitRepoSummary = {
      id: snapshot.id,
      name: snapshot.name,
      path: snapshot.path,
      currentBranch: snapshot.currentBranch,
      headHash: snapshot.headHash,
    }
    if (index === -1) {
      repos.value = [...repos.value, summary]
    } else {
      const next = [...repos.value]
      next[index] = summary
      repos.value = next
    }
    if (selectedRepoId.value === '') {
      selectedRepoId.value = snapshot.id
      saveSelectedRepoId(workspaceId.value, snapshot.id)
    } else {
      const selected = repos.value.find((r) => r.id === selectedRepoId.value) ?? null
      if (!selected) {
        const byPath = repos.value.find((r) => r.path === snapshot.path) ?? null
        selectedRepoId.value = byPath?.id ?? snapshot.id
      }
    }
    const repo = currentRepo.value
    if (repo) reconcileDiffSelection(repo)
  }

  /** Keep the working-diff selection valid after fresh snapshot data. */
  function reconcileDiffSelection(repo: GitRepo): void {
    const path = viewingDiffPath.value
    if (!path) return
    const row = repo.changes.find((c) => c.path === path)
    if (!row) {
      closeDiff()
      return
    }
    if (viewingDiffStaged.value === true && row.stagedKind === null) {
      viewingDiffStaged.value = row.unstaged !== null ? false : null
    } else if (viewingDiffStaged.value === false && row.unstaged === null) {
      viewingDiffStaged.value = row.stagedKind !== null ? true : null
    }
  }

  function invalidateRepoCaches(repoPath: string): void {
    delete workingDiffCache.value[repoPath]
    if (workingDiffError.value !== null && currentRepo.value?.path === repoPath) {
      workingDiffError.value = null
    }
    for (const key of Object.keys(commitDetailsCache.value)) {
      if (key.startsWith(`${repoPath}::`)) delete commitDetailsCache.value[key]
    }
    for (const key of Object.keys(commitDetailsLoading.value)) {
      if (key.startsWith(`${repoPath}::`)) delete commitDetailsLoading.value[key]
    }
    for (const key of Object.keys(commitDetailsError.value)) {
      if (key.startsWith(`${repoPath}::`)) delete commitDetailsError.value[key]
    }
  }

  // -- lifecycle: initialize / refresh / reset / polling ----------------------------
  //
  // NOTE: `initialize` deliberately does NOT await a previous workspace's
  // in-flight refresh. The overlap guard only joins same-workspace +
  // same-generation requests, so a slow old request can never block a new
  // workspace load; its result is dropped by the `gen` / `workspaceId`
  // checks when it settles.

  async function refresh(
    options?: { silent?: boolean; withDetails?: boolean },
  ): Promise<boolean> {
    const wsId = workspaceId.value
    if (!wsId) return false
    // Workspace-safe overlap guard: an in-flight refresh is only joined when
    // it belongs to the same workspace AND generation. A newer `initialize`
    // bumps `loadGen`, so parallel old requests never block a fresh
    // workspace load — the stale task drops its result via the gen check and
    // never clears our bookkeeping (`refreshPromise !== task` in its
    // `finally`), letting us safely overwrite it below.
    if (refreshPromise && refreshFor === wsId && refreshGen === loadGen) {
      return refreshPromise
    }
    const silent = options?.silent === true
    const withDetails = options?.withDetails !== false
    const gen = loadGen
    // Deferred assignment avoids the TDZ self-reference (`refreshPromise ===
    // task` in `finally`) that `vue-tsc --build` flags for `const`.
    const taskHolder: { task?: Promise<boolean> } = {}
    const task: Promise<boolean> = (async (): Promise<boolean> => {
      if (!silent) {
        loading.value = true
        error.value = null
      }
      try {
        const res = await getGitRepos(wsId)
        if (gen !== loadGen || workspaceId.value !== wsId) return false
        applyRepoSummaries((res.repos ?? []).map(normalizeRepoSummary))
        if (!silent) {
          loading.value = false
          error.value = null
        }
        if (withDetails) {
          const selected = selectedSummary()
          if (selected) await ensureDetails(selected.path)
          if (gen !== loadGen || workspaceId.value !== wsId) return false
        }
        return true
      } catch (e: unknown) {
        if (gen !== loadGen || workspaceId.value !== wsId) return false
        const message = errorMessage(e)
        if (silent) {
          // Silent polling must never turn the whole tab into a global
          // error while existing repos are still shown.
          if (repos.value.length === 0) error.value = message
        } else {
          loading.value = false
          error.value = message
          notifications.error('Failed to load git repositories', message)
        }
        return false
      } finally {
        if (refreshPromise === taskHolder.task) {
          refreshPromise = null
          refreshFor = null
        }
      }
    })()
    taskHolder.task = task
    refreshPromise = task
    refreshFor = wsId
    refreshGen = gen
    return task
  }

  /**
   * Load (or reload) full details for one repo path. Results are cached in
   * `repoDetails`; concurrent calls for the same path are joined. With
   * `force`, working-diff/commit-detail caches are invalidated first.
   * Never throws — failures surface via `error` only when no data exists.
   */
  async function ensureDetails(
    repoPath: string,
    options?: { force?: boolean; silent?: boolean },
  ): Promise<boolean> {
    const wsId = workspaceId.value
    if (!wsId || !repoPath) return false
    if (detailsInFlight.has(repoPath)) return false
    if (!options?.force && repoDetails.value[repoPath]) return true
    const gen = loadGen
    detailsInFlight.add(repoPath)
    detailsLoading.value = { ...detailsLoading.value, [repoPath]: true }
    try {
      if (options?.force) invalidateRepoCaches(repoPath)
      const res = await getGitRepo(wsId, repoPath)
      if (gen !== loadGen || workspaceId.value !== wsId) return false
      applyRepoSnapshot(normalizeRepoSnapshot(res.snapshot))
      return true
    } catch (e: unknown) {
      if (gen !== loadGen || workspaceId.value !== wsId) return false
      const message = errorMessage(e)
      if (!options?.silent && Object.keys(repoDetails.value).length === 0) {
        error.value = message
        notifications.error('Failed to load git repository', message)
      }
      return false
    } finally {
      detailsInFlight.delete(repoPath)
      if (gen === loadGen && workspaceId.value === wsId) {
        const next = { ...detailsLoading.value }
        delete next[repoPath]
        detailsLoading.value = next
      }
    }
  }

  async function initialize(id: string): Promise<void> {
    loadGen += 1
    detailsInFlight.clear()
    workspaceId.value = id
    clearLastConflict()
    repos.value = []
    repoDetails.value = {}
    detailsLoading.value = {}
    selectedRepoId.value = ''
    viewingDiffPath.value = null
    viewingDiffStaged.value = null
    expandedCommitHash.value = null
    expandedFilePath.value = null
    commitDetailsCache.value = {}
    commitDetailsLoading.value = {}
    commitDetailsError.value = {}
    workingDiffCache.value = {}
    workingDiffLoading.value = false
    workingDiffError.value = null
    useGitWorkingDiffSeq.invalidate()
    historyLoading.value = false
    busyOperation.value = null
    error.value = null
    await refresh()
  }

  function stopPolling(): void {
    if (summaryTimer !== null) {
      clearInterval(summaryTimer)
      summaryTimer = null
    }
    if (detailsTimer !== null) {
      clearInterval(detailsTimer)
      detailsTimer = null
    }
    polling.value = false
    if (visibilityListener !== null && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', visibilityListener)
      visibilityListener = null
    }
  }

  /** True while a summaries refresh for the current workspace is in flight. */
  function isRefreshInFlight(): boolean {
    return (
      refreshPromise !== null &&
      refreshFor === workspaceId.value &&
      refreshGen === loadGen
    )
  }

  /** Silent summaries-only refresh: details rows are never overwritten. */
  function pollSummaries(): void {
    if (!workspaceId.value || isRefreshInFlight() || busyOperation.value !== null) return
    void refresh({ silent: true, withDetails: false })
  }

  /** Silent reload of the selected repo's details (slow timer). */
  async function pollDetails(): Promise<void> {
    const wsId = workspaceId.value
    if (!wsId || busyOperation.value !== null) return
    const selected = selectedSummary()
    if (!selected || !repoDetails.value[selected.path]) return
    const gen = loadGen
    const repoPath = selected.path
    try {
      const res = await getGitRepo(wsId, repoPath)
      if (gen !== loadGen || workspaceId.value !== wsId) return
      if (selectedSummary()?.path !== repoPath) return
      // Reload the snapshot but keep diff/commit caches: an open viewer
      // must not lose its hunks on a background refresh.
      applyRepoSnapshot(normalizeRepoSnapshot(res.snapshot))
    } catch {
      // Silent polling keeps existing details visible.
    }
  }

  function tabVisible(): boolean {
    return typeof document === 'undefined' || document.visibilityState !== 'hidden'
  }

  function startPolling(
    intervalMs: number = GIT_POLL_INTERVAL_MS,
    detailsIntervalMs: number = GIT_DETAILS_POLL_MS,
  ): void {
    if (summaryTimer !== null || detailsTimer !== null) return
    polling.value = true
    if (typeof document !== 'undefined') {
      visibilityListener = () => {
        if (document.visibilityState === 'visible' && workspaceId.value) {
          void refresh({ silent: true, withDetails: false })
        }
      }
      document.addEventListener('visibilitychange', visibilityListener)
    }
    summaryTimer = setInterval(() => {
      if (!tabVisible()) return
      pollSummaries()
    }, intervalMs)
    detailsTimer = setInterval(() => {
      if (!tabVisible()) return
      pollDetails()
    }, detailsIntervalMs)
  }

  function reset(): void {
    loadGen += 1
    stopPolling()
    detailsInFlight.clear()
    workspaceId.value = null
    repos.value = []
    repoDetails.value = {}
    detailsLoading.value = {}
    selectedRepoId.value = ''
    loading.value = false
    error.value = null
    busyOperation.value = null
    historyLoading.value = false
    clearLastConflict()
    // Drop the bookkeeping for any in-flight snapshot; its gen-guarded
    // result is ignored when it settles. The op queue is replaced (not
    // awaited) so queued pre-reset mutations can no longer clear state:
    // every queued task re-checks the generation before touching state.
    refreshPromise = null
    refreshFor = null
    refreshGen = -1
    opQueue = Promise.resolve()
    viewingDiffPath.value = null
    viewingDiffStaged.value = null
    useGitWorkingDiffSeq.invalidate()
    workingDiffLoading.value = false
    workingDiffError.value = null
    workingDiffCache.value = {}
    expandedCommitHash.value = null
    expandedFilePath.value = null
    commitDetailsCache.value = {}
    commitDetailsLoading.value = {}
    commitDetailsError.value = {}
  }

  // -- lazy loaders -------------------------------------------------------------------

  /** Load staged/unstaged file diffs for the current repo (cached per repo). */
  async function loadWorkingDiffForCurrentRepo(options?: {
    force?: boolean
  }): Promise<void> {
    const wsId = workspaceId.value
    const repo = currentRepo.value
    if (!wsId || !repo) return
    const repoPath = repo.path
    if (!options?.force && workingDiffCache.value[repoPath]) return
    const gen = loadGen
    const req = useGitWorkingDiffSeq.next()
    workingDiffLoading.value = true
    workingDiffError.value = null
    try {
      const res = await getGitWorkingDiff(wsId, repoPath)
      // Repo-/request-safe: drop results when the workspace changed, the
      // selection moved to another repo, the diff was closed meanwhile, or a
      // newer diff request superseded this one.
      if (
        !useGitWorkingDiffSeq.isCurrent(req) ||
        gen !== loadGen ||
        workspaceId.value !== wsId ||
        currentRepo.value?.path !== repoPath ||
        viewingDiffPath.value === null
      ) {
        return
      }
      workingDiffCache.value[repoPath] = {
        staged: (res.diff?.staged ?? []).map(normalizeCommitFile),
        unstaged: (res.diff?.unstaged ?? []).map(normalizeCommitFile),
      }
    } catch (e: unknown) {
      if (
        !useGitWorkingDiffSeq.isCurrent(req) ||
        gen !== loadGen ||
        workspaceId.value !== wsId ||
        currentRepo.value?.path !== repoPath ||
        viewingDiffPath.value === null
      ) {
        return
      }
      workingDiffError.value = errorMessage(e)
    } finally {
      // Only the latest request for the current selection may clear the
      // loading flag — a stale loser must never flip loading false.
      if (
        useGitWorkingDiffSeq.isCurrent(req) &&
        gen === loadGen &&
        workspaceId.value === wsId &&
        currentRepo.value?.path === repoPath &&
        viewingDiffPath.value !== null
      ) {
        workingDiffLoading.value = false
      }
    }
  }

  /**
   * Retry the working diff after an error (bypasses the per-repo cache so a
   * failed load actually re-requests).
   */
  function retryWorkingDiff(): Promise<void> {
    const repo = currentRepo.value
    if (repo) delete workingDiffCache.value[repo.path]
    return loadWorkingDiffForCurrentRepo({ force: true })
  }

  /** Load commit details for the current repo+hash (cached per repo+hash). */
  async function ensureCommitDetails(hash: string): Promise<void> {
    const wsId = workspaceId.value
    const repo = currentRepo.value
    if (!wsId || !repo) return
    const key = commitDetailsKey(repo.path, hash)
    if (commitDetailsCache.value[key]) return
    const gen = loadGen
    commitDetailsLoading.value[key] = true
    commitDetailsError.value[key] = null
    try {
      const res = await getGitCommitDetails(wsId, repo.path, hash)
      if (gen !== loadGen || workspaceId.value !== wsId) return
      commitDetailsCache.value[key] = normalizeCommitDetails(res.details)
    } catch (e: unknown) {
      if (gen !== loadGen || workspaceId.value !== wsId) return
      commitDetailsError.value[key] = errorMessage(e)
    } finally {
      if (gen === loadGen && workspaceId.value === wsId) {
        commitDetailsLoading.value[key] = false
      }
    }
  }

  // -- typed mutation helper ---------------------------------------------------------------

  interface OperationCallbacks {
    successTitle?: string
    successMessage?: string
    errorTitle?: string
  }

  /**
   * Run one typed mutation against the selected repo: serialized, with
   * busy state, snapshot application (including 409 conflict snapshots)
   * and notifications. Never throws — returns true on success.
   *
   * Only a true merge conflict sets `lastConflict`: HTTP 409 with
   * `code === 'conflict'` AND a single-repo snapshot in the payload
   * (`conflictSnapshotOf(e.data)`). Then the fresh snapshot is applied,
   * a conflict warning is toasted and `lastConflict` is set (false is
   * still returned, so the boolean contract is unchanged — callers check
   * `lastConflict`). Every other 409 (runner_offline, workspace_conflict,
   * generic conflict without snapshot, …) is a normal error: no snapshot
   * is applied, `lastConflict` stays false and an error toast is shown.
   */
  async function runOperation(
    label: string,
    build: (repo: GitRepo) => GitOperationRequest,
    callbacks?: OperationCallbacks,
    options?: { silent?: boolean },
  ): Promise<boolean> {
    const silent = options?.silent === true
    const wsId = workspaceId.value
    const repo = currentRepo.value
    if (!wsId || !repo) {
      if (!silent) notifications.error('No repository selected')
      return false
    }
    const repoPath = repo.path
    let payload: GitOperationRequest
    try {
      payload = build(repo)
    } catch (e: unknown) {
      if (!silent) notifications.error(callbacks?.errorTitle ?? 'Git operation failed', errorMessage(e))
      return false
    }
    // Capture the generation: `reset()` bumps `loadGen` and replaces the
    // queue, so a task queued before a reset must not touch post-reset
    // state (including clearing a newer operation's busy label).
    const gen = loadGen
    const task = opQueue.then(async (): Promise<boolean> => {
      if (gen !== loadGen || workspaceId.value !== wsId) return false
      const current = repos.value.find((r) => r.path === repoPath) ?? null
      if (!current) {
        if (!silent) notifications.error('No repository selected')
        return false
      }
      clearLastConflict()
      busyOperation.value = label
      try {
        const res = await runGitOperation(wsId, { ...payload, repo_path: repoPath })
        if (gen !== loadGen || workspaceId.value !== wsId) return false
        applyRepoSnapshot(normalizeRepoSnapshot(res.snapshot))
        invalidateRepoCaches(repoPath)
        const updated = currentRepo.value?.path === repoPath ? currentRepo.value : null
        if (updated) reconcileDiffSelection(updated)
        if (expandedCommitHash.value) {
          // Details were invalidated above; reload the expanded commit.
          void ensureCommitDetails(expandedCommitHash.value)
        } else if (viewingDiffPath.value) {
          void loadWorkingDiffForCurrentRepo()
        }
        if (callbacks?.successTitle) {
          notifications.success(callbacks.successTitle, callbacks.successMessage)
        }
        return true
      } catch (e: unknown) {
        if (gen !== loadGen || workspaceId.value !== wsId) return false
        if (e instanceof ApiRequestError && e.status === 409 && e.code === 'conflict') {
          const snapshot = conflictSnapshotOf(e.data)
          if (snapshot) {
            applyRepoSnapshot(normalizeRepoSnapshot(snapshot))
            invalidateRepoCaches(repoPath)
            const updated = currentRepo.value?.path === repoPath ? currentRepo.value : null
            if (updated) reconcileDiffSelection(updated)
            lastConflict.value = true
            if (!silent) notifications.warning('Merge conflict', errorMessage(e))
            return false
          }
        }
        if (!silent) notifications.error(callbacks?.errorTitle ?? 'Git operation failed', errorMessage(e))
        return false
      } finally {
        // Only the owning generation may clear the busy label: a reset (or a
        // workspace switch that replaced the queue) must not wipe a newer
        // operation's state via a stale `finally`.
        if (gen === loadGen && workspaceId.value === wsId) {
          busyOperation.value = null
        }
      }
    })
    opQueue = task.then(
      () => undefined,
      () => undefined,
    )
    return task
  }

  // -- actions: selection & diff view -----------------------------------------

  function selectRepo(id: string): void {
    const summary = summaryForId(id)
    if (!summary || selectedRepoId.value === id) return
    const gen = loadGen
    selectedRepoId.value = id
    saveSelectedRepoId(workspaceId.value, id)
    closeDiff()
    closeCommitDetails()
    // Fire-and-forget lazy details for the new selection (gen-guarded:
    // a workspace switch/reset before settlement drops the result).
    void ensureDetails(summary.path).then((ok) => {
      if (gen !== loadGen) return
      if (ok) void fetchRemote({ silent: true })
      const detail = currentRepo.value
      if (detail) reconcileDiffSelection(detail)
    })
  }

  /**
   * Open a working-tree diff. `staged` selects the side when the same path
   * exists staged AND unstaged (omit for legacy single-side behaviour).
   */
  async function openDiff(path: string, staged?: boolean): Promise<void> {
    // Working-tree and commit diffs are mutually exclusive.
    expandedFilePath.value = null
    viewingDiffPath.value = path
    viewingDiffStaged.value = staged ?? null
    await loadWorkingDiffForCurrentRepo()
  }

  function closeDiff(): void {
    viewingDiffPath.value = null
    viewingDiffStaged.value = null
    // Invalidate any in-flight working-diff request so its late settlement
    // can neither populate the (now cleared) selection nor flip loading.
    useGitWorkingDiffSeq.invalidate()
    workingDiffLoading.value = false
  }

  // -- actions: commit details (expandable rows) --------------------------------

  async function toggleCommitDetails(hash: string): Promise<void> {
    if (expandedCommitHash.value === hash) {
      closeCommitDetails()
      return
    }
    expandedCommitHash.value = hash
    // Switching commits clears the previously selected file.
    expandedFilePath.value = null
    await ensureCommitDetails(hash)
  }

  function closeCommitDetails(): void {
    expandedCommitHash.value = null
    expandedFilePath.value = null
  }

  function selectCommitFile(path: string | null): void {
    // Commit and working-tree diffs are mutually exclusive.
    viewingDiffPath.value = null
    viewingDiffStaged.value = null
    useGitWorkingDiffSeq.invalidate()
    workingDiffLoading.value = false
    expandedFilePath.value = path
  }

  function setCdvHeight(h: number): void {
    const clamped = Math.min(CDV_HEIGHT_MAX, Math.max(CDV_HEIGHT_MIN, h))
    cdvHeight.value = clamped
    persist(CDV_HEIGHT_KEY, String(clamped))
  }

  // -- actions: staging --------------------------------------------------------

  function stage(path: string): Promise<boolean> {
    return runOperation('stage', () => ({ operation: 'stage', paths: [path] }), {
      errorTitle: 'Stage failed',
    })
  }

  function unstage(path: string): Promise<boolean> {
    return runOperation('unstage', () => ({ operation: 'unstage', paths: [path] }), {
      errorTitle: 'Unstage failed',
    })
  }

  function stageAll(): Promise<boolean> {
    const paths = unstagedChanges.value
      .filter((c) => c.conflict === null)
      .map((c) => c.path)
    if (paths.length === 0) return Promise.resolve(true)
    return runOperation('stage', () => ({ operation: 'stage', paths }), {
      errorTitle: 'Stage all failed',
    })
  }

  function unstageAll(): Promise<boolean> {
    const paths = stagedChanges.value.map((c) => c.path)
    if (paths.length === 0) return Promise.resolve(true)
    return runOperation('unstage', () => ({ operation: 'unstage', paths }), {
      errorTitle: 'Unstage all failed',
    })
  }

  function discard(path: string): Promise<boolean> {
    return runOperation(
      'discard',
      () => ({ operation: 'discard', paths: [path] }),
      {
        successTitle: 'Discarded changes',
        successMessage: path,
        errorTitle: 'Discard failed',
      },
    )
  }

  // -- actions: commit & push --------------------------------------------------

  /**
   * Commit staged changes. When `andPush` is set, the push only runs after
   * the commit succeeded. Returns true on success.
   */
  async function commit(message: string, andPush = false): Promise<boolean> {
    const repo = currentRepo.value
    if (!repo) {
      notifications.error('No repository selected')
      return false
    }
    const trimmed = message.trim()
    if (!trimmed) {
      notifications.error('Commit message is empty')
      return false
    }
    if (stagedChanges.value.length === 0) {
      notifications.error('No staged changes to commit')
      return false
    }
    const branchLabel = repo.currentBranch ?? 'detached HEAD'
    const ok = await runOperation(
      'commit',
      () => ({ operation: 'commit', message: trimmed }),
      {
        successTitle: `Committed on ${branchLabel}`,
        errorTitle: 'Commit failed',
      },
    )
    if (ok && andPush) return push()
    return ok
  }

  function push(): Promise<boolean> {
    const branch = currentBranch.value
    if (!currentRepo.value) {
      notifications.error('No repository selected')
      return Promise.resolve(false)
    }
    if (!branch) {
      notifications.error('Cannot push while HEAD is detached')
      return Promise.resolve(false)
    }
    // A local branch without an upstream reports ahead === 0 but still
    // needs a first publish (`push -u`). Only a tracked branch that is
    // not ahead is truly up to date.
    if (branch.ahead === 0 && branch.upstream) {
      notifications.info('Everything up to date')
      return Promise.resolve(true)
    }
    const name = branch.name
    // First-publish remote: prefer the snapshot defaultRemote, then
    // `origin` when configured, then the first remote; undefined when
    // the repo has no remotes (runner falls back to `origin`).
    const repo = currentRepo.value
    const defaultName = repo.defaultRemote?.replace('refs/remotes/', '').split('/')[0]
    const fallbackRemote =
      defaultName ?? (repo.remotes.includes('origin') ? 'origin' : repo.remotes[0])
    if (!branch.upstream) {
      // First publish: set the upstream. The backend falls back to
      // `origin` when no explicit remote is sent.
      const publishRemote = fallbackRemote ?? 'origin'
      return runOperation(
        'push',
        () => ({
          operation: 'push',
          ...(fallbackRemote ? { remote: fallbackRemote } : {}),
          set_upstream: true,
        }),
        {
          successTitle: `Published ${name} to ${publishRemote}`,
          errorTitle: 'Push failed',
        },
      )
    }
    return runOperation(
      'push',
      () => ({ operation: 'push', ...(fallbackRemote ? { remote: fallbackRemote } : {}) }),
      {
        successTitle: `Pushed to ${fallbackRemote ?? 'origin'}/${name}`,
        errorTitle: 'Push failed',
      },
    )
  }

  function fetchRemote(options?: { silent?: boolean }): Promise<boolean> {
    const silent = options?.silent === true
    const repo = currentRepo.value
    if (!repo) {
      if (!silent) notifications.error('No repository selected')
      return Promise.resolve(false)
    }
    // Guard: never fetch while a mutation is running, without a selection,
    // or when the repo has no remotes configured.
    if (busyOperation.value !== null) return Promise.resolve(false)
    if (repo.remotes.length === 0) return Promise.resolve(false)
    return runOperation(
      'fetch',
      () => ({ operation: 'fetch' }),
      {
        errorTitle: 'Fetch failed',
      },
      silent ? { silent: true } : undefined,
    )
  }

  function pull(): Promise<boolean> {
    if (!currentRepo.value) {
      notifications.error('No repository selected')
      return Promise.resolve(false)
    }
    return runOperation('pull', () => ({ operation: 'pull' }), {
      successTitle: 'Pulled latest changes',
      errorTitle: 'Pull failed',
    })
  }

  function sync(): Promise<boolean> {
    if (!currentRepo.value) {
      notifications.error('No repository selected')
      return Promise.resolve(false)
    }
    return runOperation('sync', () => ({ operation: 'sync' }), {
      successTitle: 'Synced with remote',
      errorTitle: 'Sync failed',
    })
  }

  // -- actions: branches -------------------------------------------------------

  function checkoutBranch(name: string): Promise<boolean> {
    return runOperation(
      'checkout_branch',
      () => ({ operation: 'checkout_branch', branch: name }),
      {
        successTitle: `Checked out ${name}`,
        errorTitle: 'Checkout failed',
      },
    )
  }

  function checkoutCommit(hash: string): Promise<boolean> {
    return runOperation(
      'checkout_commit',
      () => ({ operation: 'checkout_commit', commit: hash }),
      {
        successTitle: 'Detached HEAD',
        successMessage: `Checked out commit ${hash}`,
        errorTitle: 'Checkout failed',
      },
    )
  }

  /**
   * Track a remote branch locally (fetch + create/checkout). `localName`
   * defaults to the short branch name (last path segment of `remoteRef`).
   */
  function checkoutRemoteBranch(remoteRef: string, localName?: string): Promise<boolean> {
    const trimmed = localName?.trim() ? localName.trim() : undefined
    // Default local name: strip only the remote prefix ("origin/feat/x" -> "feat/x").
    const withoutRemote = remoteRef.includes('/')
      ? remoteRef.slice(remoteRef.indexOf('/') + 1)
      : remoteRef
    const local = trimmed ?? withoutRemote
    return runOperation(
      'checkout_remote_branch',
      () => ({
        operation: 'checkout_remote_branch',
        remote_ref: remoteRef,
        ...(trimmed ? { local_name: trimmed } : {}),
      }),
      {
        successTitle: `Checked out ${local}`,
        errorTitle: 'Checkout failed',
      },
    )
  }

  function createBranch(name: string, fromHash: string, checkout: boolean): Promise<boolean> {
    const trimmed = name.trim()
    if (!trimmed) {
      notifications.error('Branch name is empty')
      return Promise.resolve(false)
    }
    return runOperation(
      'create_branch',
      () => ({
        operation: 'create_branch',
        branch: trimmed,
        ...(fromHash ? { start_point: fromHash } : {}),
        checkout,
      }),
      {
        successTitle: checkout ? `Checked out ${trimmed}` : `Created branch ${trimmed}`,
        errorTitle: 'Create branch failed',
      },
    )
  }

  function renameBranch(oldName: string, newName: string): Promise<boolean> {
    const trimmed = newName.trim()
    if (!trimmed) {
      notifications.error('Branch name is empty')
      return Promise.resolve(false)
    }
    if (trimmed === oldName) return Promise.resolve(true)
    return runOperation(
      'rename_branch',
      () => ({
        operation: 'rename_branch',
        new_branch: trimmed,
        ...(oldName ? { old_branch: oldName } : {}),
      }),
      {
        successTitle: `Renamed branch to ${trimmed}`,
        errorTitle: 'Rename branch failed',
      },
    )
  }

  function deleteBranch(name: string): Promise<boolean> {
    return runOperation(
      'delete_branch',
      () => ({ operation: 'delete_branch', branch: name }),
      {
        successTitle: `Deleted branch ${name}`,
        errorTitle: 'Delete branch failed',
      },
    )
  }

  // -- actions: merging --------------------------------------------------------

  /** Merge the given branch into the currently checked out branch. */
  function mergeIntoCurrent(branchName: string): Promise<boolean> {
    return runOperation(
      'merge_into_current',
      (repo) => ({
        operation: 'merge_into_current',
        branch: branchName,
        ...(repo.currentBranch ? { message: `Merge branch '${branchName}' into ${repo.currentBranch}` } : {}),
      }),
      {
        successTitle: 'Merged successfully',
        errorTitle: 'Merge failed',
      },
    )
  }

  /** Merge the currently checked out branch into the given branch. */
  function mergeCurrentInto(branchName: string): Promise<boolean> {
    return runOperation(
      'merge_current_into',
      (repo) => ({
        operation: 'merge_current_into',
        target: branchName,
        ...(repo.currentBranch
          ? { message: `Merge branch '${repo.currentBranch}' into ${branchName}` }
          : {}),
      }),
      {
        successTitle: 'Merged successfully',
        errorTitle: 'Merge failed',
      },
    )
  }

  function mergeAbort(): Promise<boolean> {
    return runOperation('merge_abort', () => ({ operation: 'merge_abort' }), {
      successTitle: 'Merge aborted',
      errorTitle: 'Abort merge failed',
    })
  }

  // -- actions: history paging ------------------------------------------------------

  /**
   * Append the next history page for the selected repo (deduplicated by
   * hash) via GET /git/history/. Only the selected repo's details are
   * touched. `branchName` optionally filters server-side to one branch.
   */
  async function loadMoreHistory(branchName?: string): Promise<boolean> {
    const wsId = workspaceId.value
    const repo = currentRepo.value
    if (!wsId || !repo || !repo.hasMore || historyLoading.value) return false
    const repoPath = repo.path
    const pageSize = repo.historyLimit ?? GIT_HISTORY_LIMIT
    const skip = repo.commits.length
    const gen = loadGen
    historyLoading.value = true
    try {
      const res = await getGitHistory(wsId, repoPath, {
        limit: pageSize,
        skip,
        ...(branchName ? { branch: branchName } : {}),
      })
      if (gen !== loadGen || workspaceId.value !== wsId) return false
      const current = repoDetails.value[repoPath]
      if (!current || currentRepo.value?.path !== repoPath) return false
      const seen = new Set(current.commits.map((c) => c.hash))
      const merged = [...current.commits]
      for (const raw of res.commits ?? []) {
        const commit = normalizeCommit(raw)
        if (!seen.has(commit.hash)) {
          seen.add(commit.hash)
          merged.push(commit)
        }
      }
      // Merge ONLY history fields into the live detail row; every other
      // field (changes, branches, mergeState, …) stays untouched.
      repoDetails.value = {
        ...repoDetails.value,
        [repoPath]: {
          ...current,
          commits: merged,
          hasMore: res.has_more === true,
          historySkip: res.history_skip ?? skip,
          historyLimit: res.history_limit ?? pageSize,
        },
      }
      return true
    } catch (e: unknown) {
      if (gen !== loadGen || workspaceId.value !== wsId) return false
      notifications.error('Failed to load more history', errorMessage(e))
      return false
    } finally {
      if (gen === loadGen && workspaceId.value === wsId) historyLoading.value = false
    }
  }

  return {
    // state
    workspaceId,
    repos,
    repoDetails,
    detailsLoading,
    isDetailsLoading,
    selectedRepoId,
    loading,
    error,
    busyOperation,
    historyLoading,
    lastConflict,
    clearLastConflict,
    polling,
    viewingDiffPath,
    viewingDiffStaged,
    workingDiffLoading,
    workingDiffError,
    workingDiffCache,
    expandedCommitHash,
    expandedFilePath,
    commitDetailsCache,
    commitDetailsByHash,
    commitDetailsLoading,
    commitDetailsError,
    cdvHeight,
    // getters
    currentRepo,
    currentBranch,
    suggestedRemoteAction,
    stagedChanges,
    unstagedChanges,
    tagsByHash,
    refGroupsByHash,
    refGroupsFor,
    viewingDiffEntry,
    viewingDiffChange,
    expandedCommitDetails,
    viewingCommitFile,
    viewingCommitDiff,
    // lifecycle
    initialize,
    refresh,
    ensureDetails,
    reset,
    startPolling,
    stopPolling,
    loadMoreHistory,
    // actions
    selectRepo,
    openDiff,
    closeDiff,
    loadWorkingDiffForCurrentRepo,
    retryWorkingDiff,
    toggleCommitDetails,
    closeCommitDetails,
    selectCommitFile,
    setCdvHeight,
    isCommitExpanded,
    isCommitDetailsLoading,
    commitDetailsErrorFor,
    stage,
    unstage,
    stageAll,
    unstageAll,
    discard,
    commit,
    push,
    fetchRemote,
    pull,
    sync,
    checkoutBranch,
    checkoutCommit,
    checkoutRemoteBranch,
    createBranch,
    renameBranch,
    deleteBranch,
    mergeIntoCurrent,
    mergeCurrentInto,
    mergeAbort,
  }
})
