/**
 * Productive git REST API service.
 *
 * Typed raw (snake_case) contracts mirroring the backend git integration
 * (`backend/apps/runners/api.py` + `runner/src/git.py` / `service.py`):
 *
 * - GET `/workspaces/{id}/git/repos/` (light summaries)
 * - GET `/workspaces/{id}/git/repo/?repo_path=..` (full single-repo snapshot)
 * - GET `/workspaces/{id}/git/history/?repo_path=..&history_limit=..&history_skip=..&branch=..`
 * - GET `/workspaces/{id}/git/commits/{hash}/?repo_path=..`
 * - POST `/workspaces/{id}/git/operation/` (typed mutations only)
 * - POST `/workspaces/{id}/git/commit-message/` (small-model commit subject)
 *
 * All calls go through `services/api` (`get`/`post`) so JWT refresh,
 * org headers and `ApiRequestError` handling (incl. raw `data` payload
 * for HTTP 409 merge conflicts) apply.
 */

import type {
  GitOperationRequest,
  RawGitCommitDetailsResponse,
  RawGitCommitMessageResponse,
  RawGitHistoryResponse,
  RawGitOperationSuccess,
  RawGitRepoResponse,
  RawGitReposResponse,
} from '@opencuria/chat-core/gitTypes'
import { get, post } from './api'

// Wire contracts and conflict parsing are shared with the mobile client.
export type * from '@opencuria/chat-core/gitTypes'
export { conflictSnapshotOf } from '@opencuria/chat-core/gitModel'

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

export function runGitOperation(
  workspaceId: string,
  payload: GitOperationRequest,
): Promise<RawGitOperationSuccess> {
  return post<RawGitOperationSuccess>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/operation/`,
    payload,
  )
}

/** Suggest a commit subject for the staged (else all) changes of one repo. */
export function generateGitCommitMessage(
  workspaceId: string,
  repoPath: string,
): Promise<RawGitCommitMessageResponse> {
  return post<RawGitCommitMessageResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/git/commit-message/`,
    { repo_path: repoPath },
  )
}
