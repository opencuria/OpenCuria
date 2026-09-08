import { describe, expect, it } from 'vitest'

import {
  GIT_GRAPH_COLORS,
  GIT_GRAPH_CURVE_D,
  branchPaths,
  computeGitGraphLayout,
  computeGraphLayout,
  enforceChildBeforeParent,
  filterReachableCommits,
  getContentWidth,
  getGraphHeight,
  getWidthsAtVertices,
  vertexPixel,
} from './gitGraph'
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

describe('computeGitGraphLayout (engine port)', () => {
  it('lays out a linear history on one branch / colour', () => {
    const commits = [makeCommit('c0', ['c1'], 1), makeCommit('c1', ['c2'], 2), makeCommit('c2', [], 3)]
    const layout = computeGitGraphLayout(commits)

    expect(layout.orderedCommits.map((c) => c.hash)).toEqual(['c0', 'c1', 'c2'])
    expect(layout.vertices.map((v) => v.x)).toEqual([0, 0, 0])
    expect(layout.branches).toHaveLength(1)
    expect(layout.branches[0]!.colour).toBe(0)
    // Vertical segments only, no transitions.
    for (const seg of layout.branches[0]!.segments) {
      expect(seg.p1.x).toBe(seg.p2.x)
    }
    expect(layout.vertices.every((v) => !v.isMerge)).toBe(true)
  })

  it('places a fork on a second colour and joins back', () => {
    const commits = [
      makeCommit('main-tip', ['base'], 1),
      makeCommit('feat-tip', ['base'], 2),
      makeCommit('base', [], 3),
    ]
    const layout = computeGitGraphLayout(commits, { headHash: 'main-tip' })

    expect(layout.vertices.map((v) => v.x)).toEqual([0, 1, 0])
    expect(layout.branches).toHaveLength(2)
    expect(layout.branches.map((b) => b.colour)).toEqual([0, 1])
    expect(layout.vertices[0]!.isCurrent).toBe(true)
    // Second branch has a transition (lockedFirst records horizontal direction).
    const second = layout.branches[1]!
    expect(second.segments.some((s) => s.p1.x !== s.p2.x)).toBe(true)
  })

  it('routes a merge with first parent continuing and locked transitions', () => {
    const commits = [
      makeCommit('merge', ['main-tip', 'feat-tip'], 0),
      makeCommit('main-tip', ['base'], 2),
      makeCommit('feat-tip', ['base'], 3),
      makeCommit('base', [], 4),
    ]
    const layout = computeGitGraphLayout(commits)

    expect(layout.vertices[0]!.isMerge).toBe(true)
    expect(layout.vertices.map((v) => v.x)).toEqual([0, 0, 1, 0])
    // Merge commit spawns two branches: main line + side branch to feat-tip.
    expect(layout.branches.length).toBeGreaterThanOrEqual(2)
  })

  it('handles a merge between two already-placed vertices via parent branch', () => {
    // Diamond where the merge's second parent is already on a branch:
    // m merges a (on branch 0) and b (on branch 1).
    const commits = [
      makeCommit('m', ['a', 'b'], 0),
      makeCommit('a', ['base'], 1),
      makeCommit('b', ['base'], 2),
      makeCommit('base', [], 3),
    ]
    const layout = computeGitGraphLayout(commits)

    expect(layout.vertices[0]!.isMerge).toBe(true)
    expect(layout.vertices.map((v) => v.x)).toEqual([0, 0, 1, 0])
    // All segments reference valid vertex rows.
    for (const branch of layout.branches) {
      for (const seg of branch.segments) {
        expect(seg.p1.y).toBeGreaterThanOrEqual(0)
        expect(seg.p2.y).toBeLessThan(commits.length)
      }
    }
    // lockedFirst is always a boolean set by the layout pass.
    for (const branch of layout.branches) {
      for (const seg of branch.segments) {
        expect(typeof seg.lockedFirst).toBe('boolean')
      }
    }
    // Main-line branch segments follow the lastPoint.x < curPoint.x rule.
    const main = layout.branches[0]!
    for (const seg of main.segments) {
      expect(seg.lockedFirst).toBe(seg.p1.x < seg.p2.x)
    }
  })

  it('keeps input order when already child-before-parent (no timestamp re-sort)', () => {
    const commits = [
      makeCommit('tip', ['old'], 10),
      makeCommit('old', [], 1),
    ]
    const layout = computeGitGraphLayout(commits)
    expect(layout.orderedCommits.map((c) => c.hash)).toEqual(['tip', 'old'])
    expect(layout.vertices.map((v) => v.hash)).toEqual(['tip', 'old'])
  })

  it('repairs parent-before-child input into one continuous branch', () => {
    // a7u2v9w was listed before its child e8b7d3a in the mock: the engine
    // moves it directly before its earliest parent (like the reference,
    // which expects parents later in the list).
    const commits = [
      makeCommit('tip', ['child'], 1),
      makeCommit('child', ['parent'], 2),
      makeCommit('parent', [], 3),
    ].slice().reverse()
    const layout = computeGitGraphLayout(commits)
    expect(layout.orderedCommits.map((c) => c.hash)).toEqual([
      'tip',
      'child',
      'parent',
    ])
    expect(layout.branches).toHaveLength(1)
  })

  it('marks headHash as current and tolerates unknown parents', () => {
    const commits = [makeCommit('tip', ['missing'], 1)]
    const layout = computeGitGraphLayout(commits, { headHash: 'tip' })
    expect(layout.vertices).toHaveLength(1)
    expect(layout.vertices[0]!.isCurrent).toBe(true)
    expect(layout.branches).toHaveLength(1)
  })

  it('returns an empty layout for no commits', () => {
    const layout = computeGitGraphLayout([])
    expect(layout.vertices).toEqual([])
    expect(layout.branches).toEqual([])
    expect(layout.vertexColours).toEqual([])
    expect(layout.widthsAtVertices).toEqual([])
  })
})

describe('graph pixel helpers', () => {
  it('emits bezier curves with d=19.2 for horizontal transitions', () => {
    expect(GIT_GRAPH_CURVE_D).toBeCloseTo(19.2, 5)
    const commits = [
      makeCommit('main-tip', ['base'], 1),
      makeCommit('feat-tip', ['base'], 2),
      makeCommit('base', [], 3),
    ]
    const layout = computeGitGraphLayout(commits)
    const withCurve = layout.branches.find((b) =>
      b.segments.some((s) => s.p1.x !== s.p2.x),
    )
    expect(withCurve).toBeDefined()
    const paths = branchPaths(withCurve!, -1, 250)
    const joined = paths.map((p) => p.d).join(' ')
    expect(joined).toContain('C')
    // Curve control points use y1+d / y2-d with d=19.2.
    expect(joined).toMatch(/C\d+,\d+\.\d+ \d+,\d+\.\d+/)
  })

  it('shifts pixels below expandedIndex by expandY and grows height', () => {
    const commits = [makeCommit('c0', ['c1'], 1), makeCommit('c1', ['c2'], 2), makeCommit('c2', [], 3)]
    const plain = computeGitGraphLayout(commits)
    const expanded = computeGitGraphLayout(commits, { expandedIndex: 0, expandY: 250 })

    const p1 = vertexPixel(plain.vertices[1]!)
    const p2 = vertexPixel(expanded.vertices[1]!, 0, 250)
    expect(p2.cy - p1.cy).toBe(250)
    // First row stays in place.
    expect(vertexPixel(expanded.vertices[0]!, 0, 250).cy).toBe(vertexPixel(plain.vertices[0]!).cy)
    expect(expanded.height - plain.height).toBe(250)

    // Branch paths stretch without crashing.
    const before = branchPaths(plain.branches[0]!, -1, 250).map((p) => p.d).join(' ')
    const after = branchPaths(expanded.branches[0]!, 0, 250).map((p) => p.d).join(' ')
    expect(after).not.toBe(before)
  })

  it('reuses colours after a branch ends', () => {
    // Two disjoint sequential histories: the second reuses colour 0 since
    // getAvailableColour finds startAt > availableColours[0].
    const commits = [
      makeCommit('a-tip', ['root1'], 1),
      makeCommit('root1', [], 2),
      makeCommit('b-tip', ['root2'], 3),
      makeCommit('root2', [], 4),
    ]
    const layout = computeGitGraphLayout(commits)
    const colours = layout.branches.map((b) => b.colour)
    expect(colours).toEqual([0, 0])

    // Overlapping branches must use distinct colours instead.
    const fork = [
      makeCommit('main-tip', ['base'], 1),
      makeCommit('feat-tip', ['base'], 2),
      makeCommit('base', [], 3),
    ]
    const forkLayout = computeGitGraphLayout(fork)
    const forkColours = forkLayout.branches.map((b) => b.colour)
    expect(new Set(forkColours).size).toBe(forkColours.length)
  })

  it('computes widths / colours / content width plausibly', () => {
    const commits = [
      makeCommit('main-tip', ['base'], 1),
      makeCommit('feat-tip', ['base'], 2),
      makeCommit('base', [], 3),
    ]
    const layout = computeGitGraphLayout(commits)
    expect(layout.vertexColours).toEqual(layout.vertices.map((v) => v.colour % GIT_GRAPH_COLORS.length))
    expect(layout.widthsAtVertices).toEqual(getWidthsAtVertices(layout.vertices))
    expect(layout.contentWidth).toBe(getContentWidth(layout.vertices))
    expect(layout.contentWidth).toBeGreaterThan(0)
    expect(layout.height).toBe(getGraphHeight(3, false, 250))
    expect(GIT_GRAPH_COLORS).toHaveLength(12)
  })
})

describe('enforceChildBeforeParent', () => {
  it('leaves ordered lists untouched', () => {
    const commits = [makeCommit('tip', ['old'], 1), makeCommit('old', [], 2)]
    expect(enforceChildBeforeParent(commits).map((c) => c.hash)).toEqual([
      'tip',
      'old',
    ])
  })

  it('moves a commit before its earliest parent', () => {
    const commits = [
      makeCommit('tip', ['child'], 1),
      makeCommit('parent', [], 3),
      makeCommit('child', ['parent'], 2),
    ]
    expect(enforceChildBeforeParent(commits).map((c) => c.hash)).toEqual([
      'tip',
      'child',
      'parent',
    ])
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
