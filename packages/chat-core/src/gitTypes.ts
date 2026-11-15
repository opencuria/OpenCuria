/**
 * Git wire contracts and domain models shared by the web and mobile clients.
 *
 * Raw (snake_case) shapes mirror the backend git integration
 * (`backend/apps/runners/api.py`, `runner/src/git.py`). Domain models are
 * the camelCase projections both clients render. `headHash` is null for an
 * unborn repository; `currentBranch` is null while HEAD is detached.
 */

// ---------------------------------------------------------------------------
// Raw wire shapes (snake_case, as returned by the backend)
// ---------------------------------------------------------------------------

export interface RawGitCommitFile {
  old_path: string;
  new_path: string;
  status: string;
  additions: number;
  deletions: number;
}

export interface RawGitChange {
  path: string;
  old_path: string | null;
  status: string;
  staged: boolean;
  staged_kind: string | null;
  unstaged: string | null;
  conflict: string | null;
}

export interface RawGitCommit {
  hash: string;
  message: string;
  body: string;
  author: string;
  author_email: string;
  timestamp: string;
  author_date: string;
  committer: string;
  committer_email: string;
  committer_date: string;
  parents: string[];
}

export interface RawGitStash {
  selector: string;
  hash: string;
  base_hash: string | null;
  message: string;
  author: string;
  author_email: string;
  timestamp: string;
  author_date: string;
  committer: string;
  committer_email: string;
  committer_date: string;
}

export interface RawGitBranch {
  name: string;
  tip_hash: string;
  upstream: string | null;
  ahead: number;
  behind: number;
}

export interface RawGitRemoteRef {
  name: string;
  tip_hash: string;
}

export interface RawGitMergeState {
  merging: boolean;
  rebasing: boolean;
  cherry_picking: boolean;
}

export interface RawGitRepoSnapshot {
  id: string;
  name: string;
  path: string;
  current_branch: string | null;
  head_hash: string | null;
  branches: RawGitBranch[];
  remote_refs: RawGitRemoteRef[];
  remotes: string[];
  default_remote: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  merge_state: RawGitMergeState;
  commits: RawGitCommit[];
  stashes: RawGitStash[];
  has_more: boolean;
  history_skip: number;
  history_limit: number;
  changes: RawGitChange[];
}

/** Light per-repo summary from GET /git/repos/ (no branches/commits/changes). */
export interface RawGitRepoSummary {
  id: string;
  name: string;
  path: string;
  current_branch: string | null;
  head_hash: string | null;
}

export interface RawGitReposResponse {
  ok: boolean;
  repos: RawGitRepoSummary[];
}

export interface RawGitRepoResponse {
  ok: boolean;
  snapshot: RawGitRepoSnapshot;
}

export interface RawGitHistoryResponse {
  ok: boolean;
  repo_path: string;
  commits: RawGitCommit[];
  /** Present on the first page only; later pages carry `[]`. */
  stashes?: RawGitStash[];
  has_more: boolean;
  history_skip: number;
  history_limit: number;
}

export interface RawGitCommitDetails {
  hash: string;
  message: string;
  body: string;
  parents: string[];
  author: string;
  author_email: string;
  author_date: string;
  committer: string;
  committer_email: string;
  committer_date: string;
  file_changes: RawGitCommitFile[];
}

export interface RawGitCommitDetailsResponse {
  ok: boolean;
  repo_path: string;
  details: RawGitCommitDetails;
}

/** Successful mutation result: fresh single-repo snapshot plus extras. */
export interface RawGitOperationSuccess {
  ok: boolean;
  snapshot: RawGitRepoSnapshot;
  repo_path?: string;
  conflict?: boolean;
  stayed_on_target?: boolean;
  target?: string;
  source?: string;
  [key: string]: unknown;
}

/** HTTP 409 merge-conflict payload (still carries a fresh snapshot). */
export interface RawGitConflictPayload {
  ok: boolean;
  code: string;
  message: string;
  stderr?: string;
  snapshot?: RawGitRepoSnapshot;
  conflict?: boolean;
  stayed_on_target?: boolean;
  target?: string;
  source?: string;
  [key: string]: unknown;
}

export interface RawGitCommitMessageResponse {
  message: string;
}

/** Content of one file side; `revision` is a compare-and-swap token. */
export interface RawGitFileContent {
  ok: boolean;
  content: string;
  revision: string;
  exists: boolean;
}

// ---------------------------------------------------------------------------
// Typed mutation request (POST /git/operation/)
// ---------------------------------------------------------------------------

export type GitOperationName =
  | "stage"
  | "unstage"
  | "discard"
  | "stage_patch"
  | "unstage_patch"
  | "discard_patch"
  | "commit"
  | "fetch"
  | "pull"
  | "push"
  | "sync"
  | "checkout_branch"
  | "checkout_commit"
  | "checkout_remote_branch"
  | "create_branch"
  | "rename_branch"
  | "delete_branch"
  | "merge_into_current"
  | "merge_current_into"
  | "merge_abort"
  | "stash_apply"
  | "stash_pop"
  | "stash_drop"
  | "stash_branch";

export interface GitOperationRequest {
  operation: GitOperationName;
  /** Absolute repo path under /workspace. */
  repo_path: string;
  paths?: string[];
  /** Repo-relative file and unified hunks for a guarded partial operation. */
  path?: string;
  patch?: string;
  expected_index_revision?: string;
  expected_worktree_revision?: string;
  /** Original HEAD blob guard and rename source for partial unstaging. */
  expected_head_revision?: string;
  old_path?: string;
  message?: string;
  branch?: string;
  commit?: string;
  /** Stash selector (`stash@{n}`) for stash_apply/pop/drop/branch. */
  stash?: string;
  target?: string;
  new_branch?: string;
  old_branch?: string;
  start_point?: string;
  remote?: string;
  remote_ref?: string;
  local_name?: string;
  checkout?: boolean;
  set_upstream?: boolean;
}

/** Git source of one file side: index, worktree, head or a commit-ish. */
export type GitFileRevision = "index" | "worktree" | "head" | (string & {});

// ---------------------------------------------------------------------------
// Domain models (camelCase)
// ---------------------------------------------------------------------------

export type GitFileStatus = "M" | "A" | "D" | "R" | "C" | "U";

/** Staged-side change kind (index vs HEAD), null when no staged change. */
export type GitStagedKind = "M" | "A" | "D" | "R" | "C" | "U" | null;

/** Unstaged-side change kind (worktree vs index), incl. untracked marker. */
export type GitUnstagedKind = "M" | "D" | "R" | "C" | "U" | "untracked" | null;

export interface GitFileChange {
  path: string;
  oldPath: string | null;
  status: GitFileStatus;
  /** True when a staged-side change exists for this path. */
  staged: boolean;
  /** Staged-side kind (null when no staged change). */
  stagedKind: GitStagedKind;
  /** Unstaged-side kind (null when clean; 'untracked' for new files). */
  unstaged: GitUnstagedKind;
  /** Merge conflict code (e.g. 'UU', 'AA', 'UD'), null when not conflicted. */
  conflict: string | null;
}

export interface GitCommit {
  hash: string;
  message: string;
  author: string;
  /** ISO date string (author date). */
  timestamp: string;
  /** Parent commit hashes — first entry is the first parent. */
  parents: string[];
  /** Author email, if known. */
  email?: string;
  authorEmail?: string;
  authorDate?: string;
  committer?: string;
  committerEmail?: string;
  committerDate?: string;
  /** Full commit message body beyond the subject line. */
  body?: string;
  /**
   * Stash metadata when this row is a synthetic stash node (exactly one
   * row per stash entry, spliced above its base commit — never the raw
   * WIP/index/untracked commits). Mirrors vscode-git-graph's
   * `GitCommit.stash` (`GitCommitStash`: selector/baseHash/...).
   */
  stash?: GitStashInfo | null;
}

/**
 * One stash entry (`stash@{n}`), enumerated separately from the commit
 * log. `hash` is the WIP commit, `baseHash` its first parent (the commit
 * the stash was created on). Index/untracked sub-commits never surface.
 */
export interface GitStashInfo {
  /** Selector, e.g. `stash@{0}`. */
  selector: string;
  /** WIP commit hash. */
  hash: string;
  /** Base commit hash (first stash parent), null when unknown. */
  baseHash: string | null;
  /** Stash message (the `WIP on …` subject). */
  message: string;
  author: string;
  authorEmail: string;
  /** ISO date string. */
  timestamp: string;
}

export type GitCommitFileStatus = "A" | "M" | "D" | "R" | "C" | "U";

export interface GitCommitFile {
  oldPath: string;
  newPath: string;
  status: GitCommitFileStatus;
  additions: number;
  deletions: number;
}

export interface GitCommitDetails {
  hash: string;
  /** Subject line. */
  message: string;
  parents: string[];
  author: string;
  authorEmail: string;
  /** ISO date string. */
  authorDate: string;
  committer: string;
  committerEmail: string;
  /** ISO date string. */
  committerDate: string;
  body: string;
  fileChanges: GitCommitFile[];
}

export interface GitBranch {
  name: string;
  tipHash: string;
  upstream: string | null;
  ahead: number;
  behind: number;
}

/** A remote-tracking ref such as origin/main. */
export interface GitRemoteRef {
  name: string;
  tipHash: string;
}

export interface GitMergeState {
  merging: boolean;
  rebasing: boolean;
  cherryPicking: boolean;
}

export interface GitRepoSummary {
  id: string;
  name: string;
  /** Absolute path inside the workspace, e.g. /workspace/opencuria. */
  path: string;
  /** Current branch name, or null when HEAD is detached. */
  currentBranch: string | null;
  /** Resolved HEAD commit hash, null for unborn repos (no commits yet). */
  headHash: string | null;
}

export interface GitRepo {
  id: string;
  name: string;
  /** Absolute path inside the workspace, e.g. /workspace/opencuria. */
  path: string;
  /** Current branch name, or null when HEAD is detached. */
  currentBranch: string | null;
  /** Resolved HEAD commit hash, null for unborn repos (no commits yet). */
  headHash: string | null;
  branches: GitBranch[];
  remoteRefs: GitRemoteRef[];
  remotes: string[];
  defaultRemote: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  mergeState: GitMergeState;
  /** Commits sorted newest first. */
  commits: GitCommit[];
  /** Stash entries newest first (separate from commits, unpaginated). */
  stashes: GitStashInfo[];
  hasMore: boolean;
  historySkip: number;
  historyLimit: number;
  changes: GitFileChange[];
}

/** Branch / remote-ref tag pointing at a commit (graph helper). */
export interface GitRefTag {
  name: string;
  remote: boolean;
  current: boolean;
}
