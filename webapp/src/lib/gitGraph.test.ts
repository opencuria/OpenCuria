import { describe, expect, it } from 'vitest'

import { computeGraphLayout, filterReachableCommits } from './gitGraph'
import type { GitCommit } from '@/types/git'

function makeCommit(
  hash: string,
  parents: string[],
  hoursAgo: number,
): GitCommit {
  return {
    hash,
    message: `commit ${hash}`,
    author: 'Test Author',
    timestamp: new Date(Date.now() - hoursAgo * 3_600_000).toISOString(),
    parents,
  }
}

describe('computeGraphLayout', () => {
  it('lays out a linear history in a single lane, newest first', () => {
    const commits = [
      makeCommit('new', ['mid'], 1),
      makeCommit('mid', ['old'], 2),
      makeCommit('old', [], 3),
    ]

    const layout = computeGraphLayout(commits)

    expect(layout.rows.map((r) => r.commit.hash)).toEqual(['new', 'mid', 'old'])
    expect(layout.rows.every((r) => r.lane === 0)).toBe(true)
    expect(layout.laneCount).toBe(1)
    expect(layout.edges).toHaveLength(2)
    expect(
      layout.edges.every(
        (e) => e.fromLane === e.toLane && e.curved === false,
      ),
    ).toBe(true)
  })

  it('assigns a second lane for a forked branch with a curved join edge', () => {
    const commits = [
      makeCommit('main-tip', ['base'], 1),
      makeCommit('feat-tip', ['base'], 2),
      makeCommit('base', [], 3),
    ]

    const layout = computeGraphLayout(commits)

    expect(layout.laneCount).toBe(2)
    const featRow = layout.rows.find((r) => r.commit.hash === 'feat-tip')
    const baseRow = layout.rows.find((r) => r.commit.hash === 'base')
    expect(featRow?.lane).toBe(1)
    expect(baseRow?.lane).toBe(0)

    const joinEdge = layout.edges.find(
      (e) => e.fromRow === layout.rows.indexOf(featRow!),
    )
    expect(joinEdge?.curved).toBe(true)
    expect(joinEdge?.toLane).toBe(0)
  })

  it('routes merge parents into their own lane and back', () => {
    const commits = [
      makeCommit('merge', ['main-tip', 'feat-tip'], 0),
      makeCommit('main-tip', ['base'], 2),
      makeCommit('feat-tip', ['base'], 3),
      makeCommit('base', [], 4),
    ]

    const layout = computeGraphLayout(commits)

    expect(layout.rows.map((r) => r.commit.hash)).toEqual([
      'merge',
      'main-tip',
      'feat-tip',
      'base',
    ])
    expect(layout.laneCount).toBe(2)
    expect(layout.edges).toHaveLength(4)

    const mergeRow = 0
    const featRowIndex = layout.rows.findIndex(
      (r) => r.commit.hash === 'feat-tip',
    )
    const mergeToFeat = layout.edges.find(
      (e) => e.fromRow === mergeRow && e.toRow === featRowIndex,
    )
    expect(mergeToFeat?.curved).toBe(true)
    expect(mergeToFeat?.fromLane).toBe(0)
    expect(mergeToFeat?.toLane).toBe(1)
  })

  it('skips parents that are not in the commit list', () => {
    const commits = [makeCommit('shallow-tip', ['missing-parent'], 1)]

    const layout = computeGraphLayout(commits)

    expect(layout.rows).toHaveLength(1)
    expect(layout.edges).toHaveLength(0)
    expect(layout.laneCount).toBe(1)
  })

  it('returns an empty layout for no commits', () => {
    const layout = computeGraphLayout([])
    expect(layout.rows).toEqual([])
    expect(layout.edges).toEqual([])
    expect(layout.laneCount).toBe(0)
  })
})

describe('filterReachableCommits', () => {
  it('keeps only commits reachable from the given tip', () => {
    const commits = [
      makeCommit('main-tip', ['base'], 1),
      makeCommit('feat-tip', ['fork'], 2),
      makeCommit('fork', ['root'], 3),
      makeCommit('base', ['root'], 4),
      makeCommit('root', [], 5),
    ]

    const reachable = filterReachableCommits(commits, 'feat-tip')

    expect(reachable.map((c) => c.hash).sort()).toEqual([
      'feat-tip',
      'fork',
      'root',
    ])
  })

  it('returns nothing for an unknown tip', () => {
    const commits = [makeCommit('a', [], 1)]
    expect(filterReachableCommits(commits, 'nope')).toEqual([])
  })
})
