/**
 * Stash splicing for the git graph — port of the stash handling in
 * vscode-git-graph's `src/dataSource.ts` (`getCommits`, "Insert Stashes").
 *
 * Stashes are enumerated separately from the commit log (never via
 * `git log --all`, which would also surface the internal `index on …` /
 * `untracked files on …` parent commits as full rows). For each stash
 * entry exactly one synthetic single-parent node is spliced directly
 * above its base commit:
 *
 * - `hash`: the stash WIP commit hash
 * - `parents`: `[baseHash]` only (2nd/3rd stash parents are dropped, so
 *   no side lanes fork off to index/untracked commits)
 * - `stash`: the selector + base metadata (drives the ring node, the
 *   `stash@{n}` badge and the stash context menu)
 *
 * A stash whose base commit is not in the visible list (e.g. branch
 * filter, or not yet loaded via pagination) is skipped — like the
 * reference, which only splices when `commitLookup[baseHash]` exists.
 */

import type { GitCommit, GitStashInfo } from '@/types/git'

/** Build the synthetic single-parent row for one stash entry. */
export function stashCommitNode(stash: GitStashInfo): GitCommit {
  return {
    hash: stash.hash,
    message: stash.message,
    author: stash.author,
    email: stash.authorEmail,
    authorEmail: stash.authorEmail,
    timestamp: stash.timestamp,
    authorDate: stash.timestamp,
    committer: stash.author,
    committerEmail: stash.authorEmail,
    committerDate: stash.timestamp,
    parents: stash.baseHash ? [stash.baseHash] : [],
    body: '',
    stash: { ...stash },
  }
}

/**
 * Splice one synthetic row per stash directly above its base commit.
 * `commits` is the display-ordered base list (newest first, already
 * branch-filtered); `stashes` arrive newest first (`stash@{0}` first).
 * Stashes with an unknown/missing base are skipped. The input arrays are
 * never mutated.
 */
export function insertStashCommits(
  commits: GitCommit[],
  stashes: GitStashInfo[],
): GitCommit[] {
  if (stashes.length === 0 || commits.length === 0) return [...commits]
  const indexByHash = new Map<string, number>()
  for (let i = 0; i < commits.length; i++) {
    const hash = commits[i]?.hash
    if (hash !== undefined && !indexByHash.has(hash)) indexByHash.set(hash, i)
  }
  interface PendingStash {
    index: number
    node: GitCommit
  }
  const pending: PendingStash[] = []
  for (const stash of stashes) {
    if (!stash.baseHash) continue
    const index = indexByHash.get(stash.baseHash)
    if (index === undefined) continue
    pending.push({ index, node: stashCommitNode(stash) })
  }
  if (pending.length === 0) return [...commits]
  // Same base index: keep stash order (newest first); the reverse splice
  // below inserts them stacked, so sort ascending and splice from the end.
  pending.sort((a, b) => a.index - b.index)
  const out = [...commits]
  for (let i = pending.length - 1; i >= 0; i--) {
    const item = pending[i]!
    out.splice(item.index, 0, item.node)
  }
  return out
}

/** True when the row is a synthetic stash node (not a real commit). */
export function isStashCommit(commit: GitCommit): boolean {
  return commit.stash != null
}
