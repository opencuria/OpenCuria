// ---------------------------------------------------------------------------
// Git domain types for the side-panel Git tab (productive backend integration).
//
// CamelCase domain models normalized from the snake_case backend contract
// (see `src/services/git.api.ts` for the raw wire shapes). `headHash` is
// nullable (unborn repo / no commits yet); `currentBranch` is null when HEAD
// is detached.
// ---------------------------------------------------------------------------

export type GitFileStatus = 'M' | 'A' | 'D' | 'R' | 'C' | 'U'

/** Staged-side change kind (index vs HEAD), null when no staged change. */
export type GitStagedKind = 'M' | 'A' | 'D' | 'R' | 'C' | 'U' | null

/** Unstaged-side change kind (worktree vs index), incl. untracked marker. */
export type GitUnstagedKind = 'M' | 'D' | 'R' | 'C' | 'U' | 'untracked' | null

export interface GitDiffLine {
  type: 'context' | 'add' | 'del'
  content: string
}

export interface GitDiffHunk {
  /** Hunk header, e.g. "@@ -12,6 +12,7 @@". */
  header: string
  oldStart: number
  newStart: number
  lines: GitDiffLine[]
}

export interface GitFileChange {
  path: string
  oldPath: string | null
  status: GitFileStatus
  /** True when a staged-side change exists for this path. */
  staged: boolean
  /** Staged-side kind (null when no staged change). */
  stagedKind: GitStagedKind
  /** Unstaged-side kind (null when clean; 'untracked' for new files). */
  unstaged: GitUnstagedKind
  /** Merge conflict code (e.g. 'UU', 'AA', 'UD'), null when not conflicted. */
  conflict: string | null
  diff: GitDiffHunk[]
}

export interface GitCommit {
  hash: string
  message: string
  author: string
  /** ISO date string (author date). */
  timestamp: string
  /** Parent commit hashes — first entry is the first parent. */
  parents: string[]
  /** Author email, if known. */
  email?: string
  authorEmail?: string
  authorDate?: string
  committer?: string
  committerEmail?: string
  committerDate?: string
  /** Full commit message body beyond the subject line. */
  body?: string
}

export type GitCommitFileStatus = 'A' | 'M' | 'D' | 'R' | 'C' | 'U'

export interface GitCommitFile {
  oldPath: string
  newPath: string
  status: GitCommitFileStatus
  additions: number
  deletions: number
  binary: boolean
  truncated: boolean
  hasTextualDiff: boolean
  diff: GitDiffHunk[]
}

export interface GitCommitDetails {
  hash: string
  /** Subject line. */
  message: string
  parents: string[]
  author: string
  authorEmail: string
  /** ISO date string. */
  authorDate: string
  committer: string
  committerEmail: string
  /** ISO date string. */
  committerDate: string
  body: string
  fileChanges: GitCommitFile[]
}

export interface GitBranch {
  name: string
  tipHash: string
  upstream: string | null
  ahead: number
  behind: number
}

/** A remote-tracking ref such as origin/main. */
export interface GitRemoteRef {
  name: string
  tipHash: string
}

export interface GitMergeState {
  merging: boolean
  rebasing: boolean
  cherryPicking: boolean
}

export interface GitRepo {
  id: string
  name: string
  /** Absolute path inside the workspace, e.g. /workspace/opencuria. */
  path: string
  /** Current branch name, or null when HEAD is detached. */
  currentBranch: string | null
  /** Resolved HEAD commit hash, null for unborn repos (no commits yet). */
  headHash: string | null
  branches: GitBranch[]
  remoteRefs: GitRemoteRef[]
  remotes: string[]
  defaultRemote: string | null
  upstream: string | null
  ahead: number
  behind: number
  mergeState: GitMergeState
  /** Commits sorted newest first. */
  commits: GitCommit[]
  hasMore: boolean
  historySkip: number
  historyLimit: number
  changes: GitFileChange[]
}

/** Branch / remote-ref tag pointing at a commit (graph helper). */
export interface GitRefTag {
  name: string
  remote: boolean
  current: boolean
}
