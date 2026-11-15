/**
 * Git rules shared by the web and mobile Git panels: wire normalization,
 * change grouping, the suggested remote action and push targets. Pure
 * functions only; each client owns its own state, requests and UI.
 */
import type {
  GitBranch,
  GitCommit,
  GitCommitDetails,
  GitCommitFile,
  GitCommitFileStatus,
  GitFileChange,
  GitFileStatus,
  GitMergeState,
  GitRefTag,
  GitRemoteRef,
  GitRepo,
  GitRepoSummary,
  GitStagedKind,
  GitStashInfo,
  GitUnstagedKind,
  RawGitChange,
  RawGitCommit,
  RawGitCommitDetails,
  RawGitCommitFile,
  RawGitHistoryResponse,
  RawGitRepoSnapshot,
  RawGitRepoSummary,
  RawGitStash,
} from "./gitTypes";

/** Silent summaries polling interval (ms). */
export const GIT_POLL_INTERVAL_MS = 4000;
/** Silent selected-details polling interval (ms). */
export const GIT_DETAILS_POLL_MS = 15000;
/** Page size for per-repo history requests. */
export const GIT_HISTORY_LIMIT = 50;
/** Backend cap for `paths` in one typed operation (bulk actions batch). */
export const GIT_MAX_PATHS_PER_OPERATION = 256;
/** Throttle for silent fetches when the Git panel becomes visible (ms). */
export const GIT_AUTO_FETCH_THROTTLE_MS = 10_000;

/** Suggested remote action for the changes-section primary button. */
export type SuggestedRemoteAction =
  | "publish"
  | "push"
  | "pull"
  | "sync"
  | "none";

// ---------------------------------------------------------------------------
// Normalization (snake_case wire -> camelCase domain)
// ---------------------------------------------------------------------------

function asFileStatus(value: unknown): GitFileStatus {
  return value === "A" ||
    value === "D" ||
    value === "R" ||
    value === "C" ||
    value === "U"
    ? value
    : "M";
}

function asCommitFileStatus(value: unknown): GitCommitFileStatus {
  return value === "A" ||
    value === "M" ||
    value === "D" ||
    value === "R" ||
    value === "C" ||
    value === "U"
    ? value
    : "M";
}

function asStagedKind(value: unknown): GitStagedKind {
  return value === "M" ||
    value === "A" ||
    value === "D" ||
    value === "R" ||
    value === "C" ||
    value === "U"
    ? value
    : null;
}

function asUnstagedKind(value: unknown): GitUnstagedKind {
  return value === "M" ||
    value === "D" ||
    value === "R" ||
    value === "C" ||
    value === "U" ||
    value === "untracked"
    ? value
    : null;
}

function repoName(name: string | undefined, path: string): string {
  return name || path.split("/").filter(Boolean).pop() || path;
}

export function normalizeRepoSummary(raw: RawGitRepoSummary): GitRepoSummary {
  const path = raw.path;
  return {
    id: raw.id || path,
    name: repoName(raw.name, path),
    path,
    currentBranch: raw.current_branch ?? null,
    headHash: raw.head_hash ?? null,
  };
}

export function normalizeCommitFile(raw: RawGitCommitFile): GitCommitFile {
  return {
    oldPath: raw.old_path,
    newPath: raw.new_path,
    status: asCommitFileStatus(raw.status),
    additions: raw.additions ?? 0,
    deletions: raw.deletions ?? 0,
  };
}

export function normalizeChange(raw: RawGitChange): GitFileChange {
  return {
    path: raw.path,
    oldPath: raw.old_path ?? null,
    status: asFileStatus(raw.status),
    staged: raw.staged === true,
    stagedKind: asStagedKind(raw.staged_kind),
    unstaged: asUnstagedKind(raw.unstaged),
    conflict:
      typeof raw.conflict === "string" && raw.conflict ? raw.conflict : null,
  };
}

export function normalizeCommit(raw: RawGitCommit): GitCommit {
  return {
    hash: raw.hash,
    message: raw.message ?? "",
    body: raw.body ?? "",
    author: raw.author ?? "",
    email: raw.author_email ?? "",
    authorEmail: raw.author_email ?? "",
    timestamp: raw.timestamp || raw.author_date || "",
    authorDate: raw.author_date ?? "",
    committer: raw.committer ?? "",
    committerEmail: raw.committer_email ?? "",
    committerDate: raw.committer_date ?? "",
    parents: [...(raw.parents ?? [])],
  };
}

export function normalizeStash(raw: RawGitStash): GitStashInfo {
  return {
    selector: raw.selector,
    hash: raw.hash,
    baseHash: raw.base_hash ?? null,
    message: raw.message ?? "",
    author: raw.author ?? "",
    authorEmail: raw.author_email ?? "",
    timestamp: raw.timestamp || raw.author_date || "",
  };
}

export function normalizeCommitDetails(
  raw: RawGitCommitDetails,
): GitCommitDetails {
  return {
    hash: raw.hash,
    message: raw.message ?? "",
    parents: [...(raw.parents ?? [])],
    author: raw.author ?? "",
    authorEmail: raw.author_email ?? "",
    authorDate: raw.author_date ?? "",
    committer: raw.committer ?? "",
    committerEmail: raw.committer_email ?? "",
    committerDate: raw.committer_date ?? "",
    body: raw.body ?? "",
    fileChanges: (raw.file_changes ?? []).map(normalizeCommitFile),
  };
}

export function normalizeRepoSnapshot(raw: RawGitRepoSnapshot): GitRepo {
  const mergeState: GitMergeState = {
    merging: raw.merge_state?.merging === true,
    rebasing: raw.merge_state?.rebasing === true,
    cherryPicking: raw.merge_state?.cherry_picking === true,
  };
  const branches: GitBranch[] = (raw.branches ?? []).map((b) => ({
    name: b.name,
    tipHash: b.tip_hash,
    upstream: b.upstream ?? null,
    ahead: b.ahead ?? 0,
    behind: b.behind ?? 0,
  }));
  const remoteRefs: GitRemoteRef[] = (raw.remote_refs ?? []).map((r) => ({
    name: r.name,
    tipHash: r.tip_hash,
  }));
  return {
    id: raw.id || raw.path,
    name: repoName(raw.name, raw.path),
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
    stashes: (raw.stashes ?? []).map(normalizeStash),
    hasMore: raw.has_more === true,
    historySkip: raw.history_skip ?? 0,
    historyLimit: raw.history_limit ?? GIT_HISTORY_LIMIT,
    changes: (raw.changes ?? []).map(normalizeChange),
  };
}

/** Extract a single-repo snapshot from a 409 conflict payload, if present. */
export function conflictSnapshotOf(data: unknown): RawGitRepoSnapshot | null {
  if (typeof data !== "object" || data === null) return null;
  const snapshot = (data as { snapshot?: unknown }).snapshot;
  if (typeof snapshot !== "object" || snapshot === null) return null;
  const candidate = snapshot as Record<string, unknown>;
  // Single-repo snapshots carry `path` (+ `id`/`changes`); anything else
  // (e.g. `{repos: [...]}` aggregates) is not a single repo.
  if (typeof candidate.path !== "string") return null;
  return snapshot as RawGitRepoSnapshot;
}

/** Cache key for per-repo commit details. */
export function commitDetailsKey(repoPath: string, hash: string): string {
  return `${repoPath}::${hash}`;
}

// ---------------------------------------------------------------------------
// Derived repository state
// ---------------------------------------------------------------------------

/** Shallow projection of a summary until full details arrive. */
export function shallowGitRepo(summary: GitRepoSummary): GitRepo {
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
    stashes: [],
    hasMore: true,
    historySkip: 0,
    historyLimit: GIT_HISTORY_LIMIT,
    changes: [],
  };
}

/** The checked-out branch, or null while HEAD is detached / unknown. */
export function currentGitBranch(repo: GitRepo | null): GitBranch | null {
  if (!repo || repo.currentBranch === null) return null;
  return repo.branches.find((b) => b.name === repo.currentBranch) ?? null;
}

/**
 * Staged working-tree changes. Conflicted (`U`) rows are excluded — they
 * are unresolved and must never look committable.
 */
export function stagedGitChanges(repo: GitRepo | null): GitFileChange[] {
  return (
    repo?.changes.filter((c) => c.stagedKind !== null && c.conflict === null) ??
    []
  );
}

/**
 * Unstaged working-tree changes. A path with both sides appears here AND
 * in the staged list (one row per side); conflicts appear exactly once,
 * here.
 */
export function unstagedGitChanges(repo: GitRepo | null): GitFileChange[] {
  return repo?.changes.filter((c) => c.unstaged !== null) ?? [];
}

/** Unstaged paths that stage/discard all may touch (conflicts excluded). */
export function bulkUnstagedGitPaths(repo: GitRepo | null): string[] {
  return unstagedGitChanges(repo)
    .filter((c) => c.conflict === null)
    .map((c) => c.path);
}

/**
 * Suggested remote action for the primary remote button: always propose
 * whatever makes most sense right now (publish > sync > push > pull, none
 * when up to date / detached / busy).
 */
export function suggestRemoteAction(
  repo: GitRepo | null,
  busy: boolean,
): SuggestedRemoteAction {
  if (busy) return "none";
  if (!repo || repo.currentBranch === null) return "none";
  const branch = currentGitBranch(repo);
  if (!branch) return "none";
  if (branch.upstream == null) return "publish";
  const ahead = branch.ahead ?? 0;
  const behind = branch.behind ?? 0;
  if (ahead > 0 && behind > 0) return "sync";
  if (ahead > 0) return "push";
  if (behind > 0) return "pull";
  return "none";
}

/**
 * Remote for pushes and first publishes: the snapshot default remote, then
 * `origin` when configured, then the first remote; undefined when the repo
 * has no remotes (the runner falls back to `origin`).
 */
export function gitPushRemote(repo: GitRepo): string | undefined {
  const defaultName = repo.defaultRemote
    ?.replace("refs/remotes/", "")
    .split("/")[0];
  return (
    defaultName ??
    (repo.remotes.includes("origin") ? "origin" : repo.remotes[0])
  );
}

/** Default local name for a remote ref: strip only the remote prefix. */
export function remoteBranchLocalName(remoteRef: string): string {
  return remoteRef.includes("/")
    ? remoteRef.slice(remoteRef.indexOf("/") + 1)
    : remoteRef;
}

/** Strict `stash@{n}` selector check (client-side guard only). */
export function isValidStashSelector(selector: string): boolean {
  return /^stash@\{\d{1,9}\}$/.test(selector.trim());
}

/** Split many paths into backend-sized batches for one bulk mutation. */
export function chunkGitPaths(paths: string[]): string[][] {
  const batches: string[][] = [];
  for (
    let start = 0;
    start < paths.length;
    start += GIT_MAX_PATHS_PER_OPERATION
  ) {
    batches.push(paths.slice(start, start + GIT_MAX_PATHS_PER_OPERATION));
  }
  return batches;
}

/** Branch / remote-ref tags keyed by the commit hash they point at. */
export function gitRefTagsByHash(
  repo: GitRepo | null,
): Map<string, GitRefTag[]> {
  const map = new Map<string, GitRefTag[]>();
  if (!repo) return map;
  const add = (hash: string, tag: GitRefTag): void => {
    const list = map.get(hash) ?? [];
    list.push(tag);
    map.set(hash, list);
  };
  for (const branch of repo.branches) {
    add(branch.tipHash, {
      name: branch.name,
      remote: false,
      current: branch.name === repo.currentBranch,
    });
  }
  for (const ref of repo.remoteRefs) {
    add(ref.tipHash, { name: ref.name, remote: true, current: false });
  }
  return map;
}

/**
 * Merge one history page into a repo's details: commits deduplicated by
 * hash, stashes replaced from the first page only, every other field kept.
 */
export function mergeGitHistoryPage(
  current: GitRepo,
  page: RawGitHistoryResponse,
  skip: number,
  pageSize: number,
): GitRepo {
  const seen = new Set(current.commits.map((c) => c.hash));
  const commits = [...current.commits];
  for (const raw of page.commits ?? []) {
    const commit = normalizeCommit(raw);
    if (!seen.has(commit.hash)) {
      seen.add(commit.hash);
      commits.push(commit);
    }
  }
  const stashes =
    skip === 0 && page.stashes
      ? page.stashes.map(normalizeStash)
      : current.stashes;
  return {
    ...current,
    commits,
    stashes,
    hasMore: page.has_more === true,
    historySkip: page.history_skip ?? skip,
    historyLimit: page.history_limit ?? pageSize,
  };
}

/** Human labels for working-tree status letters. */
export const GIT_STATUS_LABELS: Record<GitFileStatus, string> = {
  M: "Modified",
  A: "Added",
  D: "Deleted",
  R: "Renamed",
  C: "Copied",
  U: "Unresolved",
};
