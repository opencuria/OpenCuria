/**
 * Git store — webapp adapter of the shared `GitController`
 * (`@opencuria/chat-core/gitController`) for the side-panel Git tab.
 *
 * The controller owns loading, polling, serialized mutations and commit
 * details of the bound workspace. This store mirrors its immutable snapshot
 * and adds web-only view state: the working or commit diff opened in the
 * editor, the commit details height and grouped ref badges.
 */

import { defineStore } from 'pinia'
import { computed, ref, shallowRef, watch } from 'vue'
import {
  GitController,
  INITIAL_GIT_SNAPSHOT,
  selectedGitRepo,
  type GitSnapshot,
  type GitTransport,
} from '@opencuria/chat-core/gitController'
import {
  bulkUnstagedGitPaths,
  commitDetailsKey,
  currentGitBranch,
  gitRefTagsByHash,
  stagedGitChanges,
  suggestRemoteAction,
  unstagedGitChanges,
} from '@opencuria/chat-core/gitModel'
import { groupRefTags, type GitRefGroup } from '@opencuria/chat-core/gitRefGroups'
import type { GitCommitDetails, GitCommitFile, GitRepo } from '@opencuria/chat-core/gitTypes'
import {
  generateGitCommitMessage,
  getGitCommitDetails,
  getGitHistory,
  getGitRepo,
  getGitRepos,
  readGitFileContent,
  runGitOperation,
} from '@/services/git.api'
import { useNotificationStore } from '@/stores/notifications'
import { useEditorStore } from '@/stores/editor'

const CDV_HEIGHT_KEY = 'opencuria:git:cdvHeight'
const CDV_HEIGHT_DEFAULT = 250
const CDV_HEIGHT_MIN = 120
const CDV_HEIGHT_MAX = 600

/** Storage key of the last selected repo path of a workspace. */
export function selectedRepoStorageKey(workspaceId: string): string {
  return `opencuria:git:selectedRepo:${workspaceId}`
}

function loadCdvHeight(): number {
  try {
    const parsed = Number.parseInt(localStorage.getItem(CDV_HEIGHT_KEY) ?? '', 10)
    if (!Number.isFinite(parsed)) return CDV_HEIGHT_DEFAULT
    return Math.min(CDV_HEIGHT_MAX, Math.max(CDV_HEIGHT_MIN, parsed))
  } catch {
    return CDV_HEIGHT_DEFAULT
  }
}

/** REST port; bindings resolve per call so tests can mock single functions. */
const transport: GitTransport = {
  repos: (workspaceId) => getGitRepos(workspaceId),
  repo: (workspaceId, repoPath) => getGitRepo(workspaceId, repoPath),
  history: (workspaceId, repoPath, options) => getGitHistory(workspaceId, repoPath, options),
  commitDetails: (workspaceId, repoPath, hash) =>
    getGitCommitDetails(workspaceId, repoPath, hash),
  operation: (workspaceId, payload) => runGitOperation(workspaceId, payload),
  commitMessage: (workspaceId, repoPath) => generateGitCommitMessage(workspaceId, repoPath),
  fileContent: (workspaceId, repoPath, path, revision) =>
    readGitFileContent(workspaceId, repoPath, path, revision),
}

export const useGitStore = defineStore('git', () => {
  const notifications = useNotificationStore()

  let controller: GitController | null = null
  let unsubscribe: (() => void) | null = null
  const snapshot = shallowRef<GitSnapshot>(INITIAL_GIT_SNAPSHOT)
  /** Workspace the store is bound to (null after `reset`). */
  const workspaceId = ref<string | null>(null)

  /** Path of the working-tree change whose diff is open, if any. */
  const viewingDiffPath = ref<string | null>(null)
  /** Selected diff side: true = staged, false = unstaged, null = closed. */
  const viewingDiffStaged = ref<boolean | null>(null)
  /** newPath (or oldPath) of the commit file selected for diff, if any. */
  const expandedFilePath = ref<string | null>(null)
  /** Commit details view height in px (persisted). */
  const cdvHeight = ref<number>(loadCdvHeight())

  // -- controller binding -------------------------------------------------------

  /**
   * Bind the store to a workspace and load/poll only while the panel is
   * visible. Switching workspaces starts from an empty snapshot.
   */
  function bind(id: string, visible: boolean): void {
    if (controller?.workspaceId !== id) {
      reset()
      const key = selectedRepoStorageKey(id)
      const bound = new GitController(transport, id, {
        notify: ({ tone, title, message }) => notifications[tone](title, message),
        selection: {
          load: async () => localStorage.getItem(key),
          save: async (path) => localStorage.setItem(key, path),
        },
      })
      unsubscribe = bound.subscribe(() => {
        snapshot.value = bound.getSnapshot()
      })
      controller = bound
      workspaceId.value = id
    }
    controller!.setVisible(visible)
  }

  /** Stop polling and drop all state (workspace switch, tools hidden). */
  function reset(): void {
    unsubscribe?.()
    controller?.dispose()
    unsubscribe = null
    controller = null
    workspaceId.value = null
    snapshot.value = INITIAL_GIT_SNAPSHOT
    closeDiff()
    expandedFilePath.value = null
  }

  // -- getters ------------------------------------------------------------------

  const repos = computed(() => snapshot.value.repos)
  const repoDetails = computed(() => snapshot.value.repoDetails)
  const detailsLoading = computed(() => snapshot.value.detailsLoading)
  const selectedRepoId = computed(() => snapshot.value.selectedRepoId)
  const loading = computed(() => snapshot.value.loading)
  const error = computed(() => snapshot.value.error)
  const busyOperation = computed(() => snapshot.value.busyOperation)
  const lastConflict = computed(() => snapshot.value.lastConflict)
  const generatingCommitMessage = computed(() => snapshot.value.generatingCommitMessage)
  const historyLoading = computed(() => snapshot.value.historyLoading)
  const expandedCommitHash = computed(() => snapshot.value.expandedCommitHash)

  const currentRepo = computed<GitRepo | null>(() => selectedGitRepo(snapshot.value))
  const currentBranch = computed(() => currentGitBranch(currentRepo.value))
  /** Primary remote action: publish > sync > push > pull, none when idle. */
  const suggestedRemoteAction = computed(() =>
    suggestRemoteAction(currentRepo.value, busyOperation.value !== null),
  )
  /** Staged changes; conflicted (`U`) rows are never committable. */
  const stagedChanges = computed(() => stagedGitChanges(currentRepo.value))
  /** Unstaged changes; a path with both sides also appears in `stagedChanges`. */
  const unstagedChanges = computed(() => unstagedGitChanges(currentRepo.value))
  /** Unstaged paths that stage/discard all may touch (conflicts excluded). */
  const bulkUnstagedPaths = computed(() => bulkUnstagedGitPaths(currentRepo.value))

  /** Local + remote ref badges per commit hash, grouped like vscode-git-graph. */
  const refGroupsByHash = computed(() => {
    const map = new Map<string, GitRefGroup[]>()
    const repo = currentRepo.value
    if (!repo) return map
    for (const [hash, tags] of gitRefTagsByHash(repo)) {
      map.set(hash, groupRefTags(tags, repo.remotes))
    }
    return map
  })

  function refGroupsFor(hash: string): GitRefGroup[] {
    return refGroupsByHash.value.get(hash) ?? []
  }

  function commitKey(hash: string): string | null {
    const repo = currentRepo.value
    return repo ? commitDetailsKey(repo.path, hash) : null
  }

  const expandedCommitDetails = computed<GitCommitDetails | null>(() => {
    const key = expandedCommitHash.value && commitKey(expandedCommitHash.value)
    return (key && snapshot.value.commitDetails[key]) || null
  })

  function isCommitDetailsLoading(hash: string): boolean {
    const key = commitKey(hash)
    return key !== null && snapshot.value.commitDetailsLoading[key] === true
  }

  function commitDetailsErrorFor(hash: string): string | null {
    const key = commitKey(hash)
    return (key && snapshot.value.commitDetailsError[key]) || null
  }

  /** Commit diff selection for the editor (hash + file), if any. */
  const viewingCommitDiff = computed<{ hash: string; file: GitCommitFile } | null>(() => {
    const details = expandedCommitDetails.value
    const path = expandedFilePath.value
    const file = path
      ? details?.fileChanges.find((f) => f.newPath === path || f.oldPath === path)
      : undefined
    return details && file ? { hash: details.hash, file } : null
  })

  // -- view state -----------------------------------------------------------------

  // A different repository closes diff and commit-file selections.
  watch(
    selectedRepoId,
    () => {
      closeDiff()
      expandedFilePath.value = null
    },
    { flush: 'sync' },
  )

  // Fresh snapshots keep the working-diff selection on an existing side.
  watch(
    currentRepo,
    (repo) => {
      const path = viewingDiffPath.value
      if (!repo || !path) return
      const row = repo.changes.find((change) => change.path === path)
      if (!row) closeDiff()
      else if (viewingDiffStaged.value === true && row.stagedKind === null)
        viewingDiffStaged.value = row.unstaged !== null ? false : null
      else if (viewingDiffStaged.value === false && row.unstaged === null)
        viewingDiffStaged.value = row.stagedKind !== null ? true : null
    },
    { flush: 'sync' },
  )

  /** Open the selected working-tree side in the workspace editor. */
  async function openDiff(path: string, staged: boolean): Promise<void> {
    // Working-tree and commit diffs are mutually exclusive.
    expandedFilePath.value = null
    viewingDiffPath.value = path
    viewingDiffStaged.value = staged
    const repo = currentRepo.value
    if (workspaceId.value && repo) {
      await useEditorStore().openDiff(
        workspaceId.value,
        repo.path,
        path,
        staged,
        repo.changes.find((change) => change.path === path)?.oldPath ?? path,
      )
    }
  }

  function closeDiff(): void {
    viewingDiffPath.value = null
    viewingDiffStaged.value = null
  }

  async function toggleCommitDetails(hash: string): Promise<void> {
    // Switching or closing commits clears the selected file.
    expandedFilePath.value = null
    await controller?.toggleCommitDetails(hash)
  }

  function closeCommitDetails(): void {
    expandedFilePath.value = null
    controller?.closeCommitDetails()
  }

  function selectCommitFile(path: string | null): void {
    closeDiff()
    expandedFilePath.value = path
    const selected = viewingCommitDiff.value
    const repo = currentRepo.value
    if (selected && path && workspaceId.value && repo) {
      void useEditorStore().openCommitDiff(
        workspaceId.value,
        repo.path,
        path,
        selected.hash,
        selected.file.oldPath,
      )
    }
  }

  function setCdvHeight(height: number): void {
    cdvHeight.value = Math.min(CDV_HEIGHT_MAX, Math.max(CDV_HEIGHT_MIN, height))
    try {
      localStorage.setItem(CDV_HEIGHT_KEY, String(cdvHeight.value))
    } catch {
      // Storage may be unavailable (private mode); the height still applies.
    }
  }

  // -- controller actions ---------------------------------------------------------

  /** Store action (seen by `$onAction`) delegating to the bound controller. */
  function mutation<A extends unknown[]>(
    run: (bound: GitController, ...args: A) => Promise<boolean>,
  ): (...args: A) => Promise<boolean> {
    return (...args) => (controller ? run(controller, ...args) : Promise.resolve(false))
  }

  return {
    // state
    workspaceId,
    repos,
    repoDetails,
    detailsLoading,
    selectedRepoId,
    loading,
    error,
    busyOperation,
    lastConflict,
    generatingCommitMessage,
    historyLoading,
    expandedCommitHash,
    viewingDiffPath,
    viewingDiffStaged,
    expandedFilePath,
    cdvHeight,
    // getters
    currentRepo,
    currentBranch,
    suggestedRemoteAction,
    stagedChanges,
    unstagedChanges,
    bulkUnstagedPaths,
    refGroupsFor,
    expandedCommitDetails,
    viewingCommitDiff,
    isCommitDetailsLoading,
    commitDetailsErrorFor,
    // lifecycle
    bind,
    reset,
    refresh: (options?: { silent?: boolean; withDetails?: boolean; force?: boolean }) =>
      controller?.refresh(options) ?? Promise.resolve(false),
    fetchAndRefresh: () => controller?.fetchAndRefresh() ?? Promise.resolve(),
    loadMoreHistory: (branch?: string) => controller?.loadMoreHistory(branch) ?? Promise.resolve(),
    generateCommitMessage: () =>
      controller?.generateCommitMessage() ?? Promise.resolve(null),
    selectRepo: (id: string) => controller?.selectRepo(id),
    // view actions
    openDiff,
    closeDiff,
    toggleCommitDetails,
    closeCommitDetails,
    selectCommitFile,
    setCdvHeight,
    // mutations
    stage: mutation((c, path: string) => c.stage(path)),
    unstage: mutation((c, path: string) => c.unstage(path)),
    stageAll: mutation((c) => c.stageAll()),
    unstageAll: mutation((c) => c.unstageAll()),
    discard: mutation((c, path: string) => c.discard(path)),
    discardAll: mutation((c, paths: string[]) => c.discardAll(paths)),
    commit: mutation((c, message: string, andPush?: boolean) => c.commit(message, andPush)),
    push: mutation((c) => c.push()),
    fetchRemote: mutation((c) => c.fetchRemote()),
    pull: mutation((c) => c.pull()),
    sync: mutation((c) => c.sync()),
    checkoutBranch: mutation((c, name: string) => c.checkoutBranch(name)),
    checkoutCommit: mutation((c, hash: string) => c.checkoutCommit(hash)),
    checkoutRemoteBranch: mutation((c, remoteRef: string, localName?: string) =>
      c.checkoutRemoteBranch(remoteRef, localName),
    ),
    createBranch: mutation((c, name: string, fromHash: string, checkout: boolean) =>
      c.createBranch(name, fromHash, checkout),
    ),
    renameBranch: mutation((c, oldName: string, newName: string) =>
      c.renameBranch(oldName, newName),
    ),
    deleteBranch: mutation((c, name: string) => c.deleteBranch(name)),
    mergeIntoCurrent: mutation((c, branch: string) => c.mergeIntoCurrent(branch)),
    mergeCurrentInto: mutation((c, target: string) => c.mergeCurrentInto(target)),
    mergeAbort: mutation((c) => c.mergeAbort()),
    applyStash: mutation((c, selector: string) => c.applyStash(selector)),
    popStash: mutation((c, selector: string) => c.popStash(selector)),
    dropStash: mutation((c, selector: string) => c.dropStash(selector)),
    createBranchFromStash: mutation((c, selector: string, branch: string) =>
      c.createBranchFromStash(selector, branch),
    ),
  }
})
