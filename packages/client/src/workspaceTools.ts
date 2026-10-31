import type {
  GitFileRevision,
  GitOperationRequest,
  RawGitCommitDetailsResponse,
  RawGitCommitMessageResponse,
  RawGitFileContent,
  RawGitHistoryResponse,
  RawGitOperationSuccess,
  RawGitRepoResponse,
  RawGitReposResponse,
} from "@opencuria/chat-core/gitTypes";
import { GIT_HISTORY_LIMIT } from "@opencuria/chat-core/gitModel";
import type { ApiClient } from "./api";
import type { RealtimeClient } from "./realtime";

function workspacePath(workspaceId: string, suffix: string): string {
  return `/workspaces/${encodeURIComponent(workspaceId)}/${suffix}`;
}

/** REST git integration of one workspace (same contract as the webapp). */
export function createGitApi(api: () => ApiClient) {
  return {
    /** Light repo summaries (no branches/commits/changes). */
    repos: (workspaceId: string, signal?: AbortSignal) =>
      api().get<RawGitReposResponse>(
        workspacePath(workspaceId, "git/repos/"),
        signal,
      ),
    /** Full single-repo snapshot (status, branches, remotes, recent history). */
    repo: (workspaceId: string, repoPath: string, signal?: AbortSignal) =>
      api().get<RawGitRepoResponse>(
        workspacePath(
          workspaceId,
          `git/repo/?${new URLSearchParams({ repo_path: repoPath })}`,
        ),
        signal,
      ),
    history: (
      workspaceId: string,
      repoPath: string,
      options: { limit?: number; skip?: number; branch?: string } = {},
      signal?: AbortSignal,
    ) => {
      const params = new URLSearchParams({
        repo_path: repoPath,
        history_limit: String(options.limit ?? GIT_HISTORY_LIMIT),
        history_skip: String(options.skip ?? 0),
      });
      if (options.branch) params.set("branch", options.branch);
      return api().get<RawGitHistoryResponse>(
        workspacePath(workspaceId, `git/history/?${params}`),
        signal,
      );
    },
    commitDetails: (
      workspaceId: string,
      repoPath: string,
      hash: string,
      signal?: AbortSignal,
    ) =>
      api().get<RawGitCommitDetailsResponse>(
        workspacePath(
          workspaceId,
          `git/commits/${encodeURIComponent(hash)}/?${new URLSearchParams({ repo_path: repoPath })}`,
        ),
        signal,
      ),
    /** Typed mutation; HTTP 409 merge conflicts still carry a fresh snapshot. */
    operation: (
      workspaceId: string,
      payload: GitOperationRequest,
      signal?: AbortSignal,
    ) =>
      api().post<RawGitOperationSuccess>(
        workspacePath(workspaceId, "git/operation/"),
        payload,
        signal,
      ),
    /** Small-model commit subject for staged (else all) changes. */
    commitMessage: (
      workspaceId: string,
      repoPath: string,
      signal?: AbortSignal,
    ) =>
      api().post<RawGitCommitMessageResponse>(
        workspacePath(workspaceId, "git/commit-message/"),
        { repo_path: repoPath },
        signal,
      ),
    /** One file side: `index`, `worktree`, `head` or a commit-ish. */
    fileContent: (
      workspaceId: string,
      repoPath: string,
      path: string,
      revision: GitFileRevision,
      signal?: AbortSignal,
    ) =>
      api().get<RawGitFileContent>(
        workspacePath(
          workspaceId,
          `git/file-content/?${new URLSearchParams({ repo_path: repoPath, path, revision })}`,
        ),
        signal,
      ),
  };
}

/**
 * Interactive PTY of one workspace. `start` dispatches the runner task; the
 * session id arrives as `terminal:started` on the workspace room. Input and
 * output are base64 encoded bytes.
 */
export function createTerminalApi(
  api: () => ApiClient,
  realtime: RealtimeClient,
) {
  return {
    start: (
      workspaceId: string,
      cols = 80,
      rows = 24,
      signal?: AbortSignal,
    ) =>
      api().post<{ task_id: string }>(
        workspacePath(workspaceId, "terminal/"),
        { cols, rows },
        signal,
      ),
    input: (workspaceId: string, terminalId: string, data: string) =>
      realtime.emit("frontend:terminal_input", {
        workspace_id: workspaceId,
        terminal_id: terminalId,
        data,
      }),
    resize: (
      workspaceId: string,
      terminalId: string,
      cols: number,
      rows: number,
    ) =>
      realtime.emit("frontend:terminal_resize", {
        workspace_id: workspaceId,
        terminal_id: terminalId,
        cols,
        rows,
      }),
    close: (workspaceId: string, terminalId: string) =>
      realtime.emit("frontend:terminal_close", {
        workspace_id: workspaceId,
        terminal_id: terminalId,
      }),
  };
}
