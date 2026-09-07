/**
 * Git graph layout computation for the side-panel Git tab.
 *
 * Pure functions: takes commits (any order) and returns row/lane
 * coordinates plus edges. The component layer converts these logical
 * coordinates to pixels — this module has no SVG/DOM knowledge.
 *
 * Lane model: while walking commits newest-first, each lane slot holds the
 * hash of the commit expected next in that lane. A commit claims the
 * leftmost slot expecting it (or a free slot), its first parent continues
 * in the same lane, and additional (merge) parents occupy their own lanes.
 * Freed slots are reused to keep the graph compact.
 */

import type { GitCommit } from '@/types/git'

/** Number of chart color tokens (--chart-1 .. --chart-5) lanes cycle through. */
export const GRAPH_LANE_COLORS = 5

export interface GitGraphRow {
  commit: GitCommit
  lane: number
  /** Chart color index (1-based) for the commit node. */
  color: number
}

export interface GitGraphEdge {
  fromRow: number
  fromLane: number
  toRow: number
  toLane: number
  /** Chart color index (1-based) of the lane the edge travels towards. */
  color: number
  /** True when the edge changes lane and should be drawn as a curve. */
  curved: boolean
}

export interface GitGraphLayout {
  rows: GitGraphRow[]
  edges: GitGraphEdge[]
  laneCount: number
}

/**
 * Compute row/lane layout for a commit list.
 *
 * Commits are sorted newest first by timestamp; mock data and locally
 * created commits guarantee parents are always older than their children.
 */
export function computeGraphLayout(commits: GitCommit[]): GitGraphLayout {
  const ordered = [...commits].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime(),
  )
  const known = new Set(ordered.map((c) => c.hash))

  // lanes[i] = hash of the commit expected next in lane i (null = free).
  const lanes: (string | null)[] = []
  const laneColors: number[] = []
  let colorCounter = 0

  const rows: GitGraphRow[] = []
  const rowByHash = new Map<string, number>()
  const laneByHash = new Map<string, number>()
  const colorByHash = new Map<string, number>()

  function nextColor(): number {
    colorCounter += 1
    return ((colorCounter - 1) % GRAPH_LANE_COLORS) + 1
  }

  /** Find the leftmost free slot, or append a new one. */
  function claimFreeLane(): number {
    const free = lanes.indexOf(null)
    if (free >= 0) return free
    lanes.push(null)
    laneColors.push(0)
    return lanes.length - 1
  }

  for (const commit of ordered) {
    // Claim the leftmost lane expecting this commit; free any duplicates.
    let lane = lanes.indexOf(commit.hash)
    if (lane === -1) {
      lane = claimFreeLane()
      laneColors[lane] = nextColor()
    }
    for (let i = lane + 1; i < lanes.length; i++) {
      if (lanes[i] === commit.hash) lanes[i] = null
    }

    const row = rows.length
    const color = laneColors[lane] ?? 1
    rows.push({ commit, lane, color })
    rowByHash.set(commit.hash, row)
    laneByHash.set(commit.hash, lane)
    colorByHash.set(commit.hash, color)

    // First parent continues in this lane; extra parents get their own.
    const [firstParent, ...mergeParents] = commit.parents
    lanes[lane] = firstParent && known.has(firstParent) ? firstParent : null
    for (const parent of mergeParents) {
      if (!known.has(parent) || lanes.includes(parent)) continue
      const parentLane = claimFreeLane()
      lanes[parentLane] = parent
      laneColors[parentLane] = nextColor()
    }
  }

  // Edges connect each commit node directly to each known parent node.
  const edges: GitGraphEdge[] = []
  for (const { commit } of rows) {
    const fromRow = rowByHash.get(commit.hash)
    const fromLane = laneByHash.get(commit.hash)
    if (fromRow === undefined || fromLane === undefined) continue
    for (const parent of commit.parents) {
      const toRow = rowByHash.get(parent)
      const toLane = laneByHash.get(parent)
      if (toRow === undefined || toLane === undefined) continue
      edges.push({
        fromRow,
        fromLane,
        toRow,
        toLane,
        color: colorByHash.get(parent) ?? 1,
        curved: toLane !== fromLane,
      })
    }
  }

  const laneCount = rows.reduce((max, row) => Math.max(max, row.lane + 1), 0)
  return { rows, edges, laneCount }
}

/**
 * Return only the commits reachable from the given tip hash (used by the
 * graph's branch filter).
 */
export function filterReachableCommits(
  commits: GitCommit[],
  tipHash: string,
): GitCommit[] {
  const byHash = new Map(commits.map((c) => [c.hash, c]))
  const reachable = new Set<string>()
  const stack = [tipHash]
  while (stack.length > 0) {
    const hash = stack.pop() as string
    if (reachable.has(hash)) continue
    const commit = byHash.get(hash)
    if (!commit) continue
    reachable.add(hash)
    stack.push(...commit.parents)
  }
  return commits.filter((c) => reachable.has(c.hash))
}
