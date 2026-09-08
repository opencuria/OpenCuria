// ---------------------------------------------------------------------------
// Git types for the side-panel Git tab.
// Currently backed by frontend mock data only (see src/mock/git.ts); the
// shapes mirror what a future backend git API would return.
// ---------------------------------------------------------------------------

export type GitFileStatus = 'M' | 'A' | 'D'

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
  status: GitFileStatus
  staged: boolean
  diff: GitDiffHunk[]
}

export interface GitCommit {
  hash: string
  message: string
  author: string
  /** ISO date string. */
  timestamp: string
  /** Parent commit hashes — first entry is the first parent. */
  parents: string[]
  /** Author email, if known. */
  email?: string
  /** Full commit message body beyond the subject line. */
  body?: string
}

export type GitCommitFileStatus = 'A' | 'M' | 'D' | 'R' | 'U'

export interface GitCommitFile {
  oldPath: string
  newPath: string
  status: GitCommitFileStatus
  additions: number
  deletions: number
  diff: GitDiffHunk[]
}

export interface GitCommitDetails {
  hash: string
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
  ahead: number
  behind: number
}

/** A remote-tracking ref such as origin/main. */
export interface GitRemoteRef {
  name: string
  tipHash: string
}

export interface GitRepo {
  id: string
  name: string
  /** Absolute path inside the workspace, e.g. /workspace/opencuria. */
  path: string
  /** Current branch name, or null when HEAD is detached. */
  currentBranch: string | null
  /** Resolved HEAD commit hash (branch tip or detached commit). */
  headHash: string
  branches: GitBranch[]
  remoteRefs: GitRemoteRef[]
  /** Commits sorted newest first. */
  commits: GitCommit[]
  changes: GitFileChange[]
}
