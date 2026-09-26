import { describe, expect, it } from 'vitest'

import { insertStashCommits, isStashCommit, stashCommitNode } from './gitStash'
import type { GitCommit, GitStashInfo } from '@/types/git'

function makeCommit(hash: string, parents: string[] = []): GitCommit {
  return {
    hash,
    message: `commit ${hash}`,
    author: 'Test Author',
    timestamp: '2026-09-26T10:00:00Z',
    parents,
  }
}

function makeStash(selector: string, hash: string, baseHash: string | null): GitStashInfo {
  return {
    selector,
    hash,
    baseHash,
    message: `WIP on main: base ${baseHash}`,
    author: 'Test Author',
    authorEmail: 'test@local',
    timestamp: '2026-09-26T11:00:00Z',
  }
}

describe('stashCommitNode', () => {
  it('builds a single-parent synthetic row', () => {
    const node = stashCommitNode(makeStash('stash@{0}', 'wip1', 'base'))
    expect(node.hash).toBe('wip1')
    expect(node.parents).toEqual(['base'])
    expect(node.stash?.selector).toBe('stash@{0}')
    expect(isStashCommit(node)).toBe(true)
    expect(isStashCommit(makeCommit('base'))).toBe(false)
  })

  it('handles a missing base with no parents', () => {
    const node = stashCommitNode(makeStash('stash@{0}', 'wip1', null))
    expect(node.parents).toEqual([])
  })
})

describe('insertStashCommits', () => {
  it('splices one row per stash directly above its base', () => {
    const commits = [makeCommit('tip', ['base']), makeCommit('base', [])]
    const out = insertStashCommits(commits, [makeStash('stash@{0}', 'wip1', 'base')])
    expect(out.map((c) => c.hash)).toEqual(['tip', 'wip1', 'base'])
    expect(out[1]!.parents).toEqual(['base'])
  })

  it('stacks several stashes on the same base newest-first', () => {
    const commits = [makeCommit('base', [])]
    const out = insertStashCommits(commits, [
      makeStash('stash@{0}', 'wip-new', 'base'),
      makeStash('stash@{1}', 'wip-old', 'base'),
    ])
    expect(out.map((c) => c.hash)).toEqual(['wip-new', 'wip-old', 'base'])
  })

  it('skips stashes whose base is not visible', () => {
    const commits = [makeCommit('tip', ['base']), makeCommit('base', [])]
    const out = insertStashCommits(commits, [makeStash('stash@{0}', 'wip1', 'missing')])
    expect(out.map((c) => c.hash)).toEqual(['tip', 'base'])
  })

  it('returns a copy and leaves inputs untouched', () => {
    const commits = [makeCommit('base', [])]
    const stashes = [makeStash('stash@{0}', 'wip1', 'base')]
    const out = insertStashCommits(commits, stashes)
    expect(out).not.toBe(commits)
    expect(commits).toHaveLength(1)
    expect(out.map((c) => c.hash)).toEqual(['wip1', 'base'])
  })

  it('handles empty inputs', () => {
    expect(insertStashCommits([], [makeStash('stash@{0}', 'w', 'b')])).toEqual([])
    const commits = [makeCommit('a', [])]
    expect(insertStashCommits(commits, []).map((c) => c.hash)).toEqual(['a'])
  })
})
