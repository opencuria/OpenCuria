/**
 * Productive git REST API service.
 *
 * Typed raw (snake_case) contracts mirroring the backend git integration
 * (`backend/apps/runners/api.py` + `runner/src/git.py` / `service.py`):
 *
 * - GET `/workspaces/{id}/git/repos/` (light summaries)
 * - GET `/workspaces/{id}/git/repo/?repo_path=..` (full single-repo snapshot)
 * - GET `/workspaces/{id}/git/history/?repo_path=..&history_limit=..&history_skip=..&branch=..`
 * - GET `/workspaces/{id}/git/diff/?repo_path=..`
 * - GET `/workspaces/{id}/git/commits/{hash}/?repo_path=..`
 * - POST `/workspaces/{id}/git/operation/` (typed fields only)
 *
 * All calls go through `services/api` (`get`/`post`) so JWT refresh,
 * org headers and `ApiRequestError` handling (incl. raw `data` payload
 * for HTTP 409 merge conflicts) apply.
 */

import { get, post } from './api'

// ---------------------------------------------------------------------------
// Raw wire shapes (snake_case, as returned by the backend)
// ---------------------------------------------------------------------------

export interface RawGitDiffLine {
  type: 'context' | 'add' | 'del'
  content: string
}

export interface RawGitDiffHunk {
  header: string
  old_start: number
  new_start: number
  lines: RawGitDiffLine[]
}

export interface RawGitCommitFile {
  old_path: string
  new_path: string
  status: string
  additions: number
  deletions: number
  binary: boolean
  truncated: boolean
  has_textual_diff: boolean
  diff: RawGitDiffHunk[]
}

export interface RawGitChange {
  path: string
  old_path: string | null
  status: string
  staged: boolean
  staged_kind: string | null
  unstaged: string | null
  conflict: string | null
  diff: RawGitDiffHunk[]
}

export interface RawGitCommit {
  hash: string
  message: string
  body: string
  author: string
  author_email: string
  timestamp: string
  author_date: string
  committer: string
  committer_email: string
  committer_date: string
  parents: string[]
}

export interface RawGitBranch {
  name: string
  tip_hash: string
  upstream: string | null
  ahead: number
  behind: number
}

export interface RawGitRemoteRef {
  name: string
  tip_hash: string
}

export interface RawGitMergeState {
  merging: boolean
  rebasing: boolean
  cherry_picking: boolean
}

export interface RawGitRepoSnapshot {
  id: string
  name: string
  path: string
  current_branch: string | null
  head_hash: string | null
  branches: RawGitBranch[]
  remote_refs: RawGitRemoteRef[]
  remotes: string[]
  default_remote: string | null
  upstream: string | null
  ahead: number
  behind: number
  merge_state: RawGitMergeState
  commits: RawGitCommit[]
  has_more: boolean
  history_skip: number
  history_limit: number
  changes: RawGitChange[]
}

/** Light per-repo summary from GET /git/repos/ (no branches/commits/changes). */
export interface RawGitRepoSummary {
  id: string
  name: string
  path: string
  current_branch: string | null
  head_hash: string | null
}

export interface RawGitReposResponse {
  ok: boolean
  repos: RawGitRepoSummary[]
}

export interface RawGitRepoResponse {
  ok: boolean
  snapshot: RawGitRepoSnapshot
}

export interface RawGitHistoryResponse {
  ok: boolean
  repo_path: string
  commits: RawGitCommit[]
  has_more: boolean
  history_skip: number
  history_limit: number
}

export interface RawGitWorkingDiff {
  staged: RawGitCommitFile[]
  unstaged: RawGitCommitFile[]
}

export interface RawGitDiffResponse {
  ok: boolean
  repo_path: string
  diff: RawGitWorkingDiff
}

export interface RawGitCommitDetails {
  hash: string
  message: string
  body: string
  parents: string[]
  author: string
  author_email: string
  author_date: string
  committer: string
  committer_email: string
  committer_date: string
  file_changes: RawGitCommitFile[]
}

export interface RawGitCommitDetailsResponse {
  ok: boolean
  repo_path: string
  details: RawGitCommitDetails
}

/** Successful mutation result: fresh single-repo snapshot plus extras. */
export interface RawGitOperationSuccess {
  ok: boolean
  snapshot: RawGitRepoSnapshot
  repo_path?: string
  conflict?: boolean
  stayed_on_target?: boolean
  target?: string
  source?: string
  [key: string]: unknown
}

/** HTTP 409 merge-conflict payload (still carries a fresh snapshot). */
export interface RawGitConflictPayload {
  ok: boolean
  code: string
  message: string
  stderr?: string
  snapshot?: RawGitRepoSnapshot
  conflict?: boolean
  stayed_on_target?: boolean
  target?: string
  source?: string
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// Typed operation request (POST /git/operation/)
// ---------------------------------------------------------------------------

export type GitOperationName =
  | 'list_repos'
  | 'repo_snapshot'
  | 'repo_history'
  | 'working_diff'
  | 'commit_details'
  | 'stage'
  | 'unstage'
  | 'discard'
  | 'commit'
  | 'fetch'
  | 'pull'
  | 'push'
  | 'sync'
  | 'checkout_branch'
  | 'checkout_commit'
  | 'checkout_remote_branch'
  | 'create_branch'
  | 'rename_branch'
  | 'delete_branch'
  | 'merge_into_current'
  | 'merge_current_into'
  | 'merge_abort'

export interface GitOperationRequest {
  operation: GitOperationName
  /** Absolute repo path under /workspace. Omitted for `list_repos`. */
  repo_path?: string | null
  paths?: string[]
  message?: string
  branch?: string
  commit?: string
  target?: string
  new_branch?: string
  old_branch?: string
  start_point?: string
  remote?: string
  remote_ref?: string
  local_name?: string
  checkout?: boolean
  set_upstream?: boolean
  history_limit?: number
  history_skip?: number
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

/** Default page size for paged history requests. */
export const GIT_HISTORY_PAGE_SIZE = 50

/** List light repo summaries (no branches/commits/changes). */
export function getGitRepos(workspaceId: string): Promise<RawGitReposResponse> {
  return get<RawGitReposResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/repos/`,
  )
}

/** Load the full single-repo snapshot (status/branches/remotes/recent history). */
export function getGitRepo(
  workspaceId: string,
  repoPath: string,
): Promise<RawGitRepoResponse> {
  const params = new URLSearchParams({ repo_path: repoPath })
  return get<RawGitRepoResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/repo/?${params.toString()}`,
  )
}

export function getGitHistory(
  workspaceId: string,
  repoPath: string,
  opts?: { limit?: number; skip?: number; branch?: string },
): Promise<RawGitHistoryResponse> {
  const params = new URLSearchParams({ repo_path: repoPath })
  params.set('history_limit', String(opts?.limit ?? GIT_HISTORY_PAGE_SIZE))
  params.set('history_skip', String(opts?.skip ?? 0))
  if (opts?.branch) params.set('branch', opts.branch)
  return get<RawGitHistoryResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/history/?${params.toString()}`,
  )
}

export function getGitWorkingDiff(
  workspaceId: string,
  repoPath: string,
): Promise<RawGitDiffResponse> {
  const params = new URLSearchParams({ repo_path: repoPath })
  return get<RawGitDiffResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/diff/?${params.toString()}`,
  )
}

export function getGitCommitDetails(
  workspaceId: string,
  repoPath: string,
  hash: string,
): Promise<RawGitCommitDetailsResponse> {
  const params = new URLSearchParams({ repo_path: repoPath })
  return get<RawGitCommitDetailsResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/commits/${encodeURIComponent(hash)}/?${params.toString()}`,
  )
}

function stripUndefined(body: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(body)) {
    if (value !== undefined) out[key] = value
  }
  return out
}

export function buildGitOperationBody(payload: GitOperationRequest): Record<string, unknown> {
  return stripUndefined({
    operation: payload.operation,
    repo_path: payload.repo_path ?? undefined,
    paths: payload.paths,
    message: payload.message,
    branch: payload.branch,
    commit: payload.commit,
    target: payload.target,
    new_branch: payload.new_branch,
    old_branch: payload.old_branch,
    start_point: payload.start_point,
    remote: payload.remote,
    remote_ref: payload.remote_ref,
    local_name: payload.local_name,
    checkout: payload.checkout,
    set_upstream: payload.set_upstream,
    history_limit: payload.history_limit,
    history_skip: payload.history_skip,
  })
}

export function runGitOperation(
  workspaceId: string,
  payload: GitOperationRequest,
): Promise<RawGitOperationSuccess> {
  return post<RawGitOperationSuccess>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/operation/`,
    buildGitOperationBody(payload),
  )
}

/** Extract a single-repo snapshot from a 409 conflict payload, if present. */
export function conflictSnapshotOf(data: unknown): RawGitRepoSnapshot | null {
  if (typeof data !== 'object' || data === null) return null
  const snapshot = (data as { snapshot?: unknown }).snapshot
  if (typeof snapshot !== 'object' || snapshot === null) return null
  const candidate = snapshot as Record<string, unknown>
  // Single-repo snapshots carry `path` (+ `id`/`changes`); anything else
  // (e.g. `{repos: [...]}` aggregates) is not a single repo.
  if (typeof candidate.path !== 'string') return null
  return snapshot as RawGitRepoSnapshot
}
