/**
 * Git graph layout engine — pure TypeScript port of the layout geometry in
 * vscode-git-graph's `web/graph.ts` (Branch/Vertex/Graph classes), without
 * any DOM/SVG rendering.
 *
 * The component layer (Phase 3) converts the returned vertices/branches to
 * SVG pixels via `vertexPixel` / `branchPaths`.
 *
 * Legacy lane API (`computeGraphLayout`, `GitGraphRow`, `GitGraphEdge`) is
 * kept below as a deprecated compatibility wrapper until Phase 3 migrates
 * `GitGraphSection.vue` and the store.
 */

import type { GitCommit } from '@/types/git'

// --- Constants (defaults from vscode-git-graph `src/config.ts`) ---

/** Horizontal grid step in px. */
export const GIT_GRAPH_GRID_X = 16
/** Vertical grid step in px. */
export const GIT_GRAPH_GRID_Y = 24
/** Horizontal graph offset in px. */
export const GIT_GRAPH_OFFSET_X = 16
/** Vertical graph offset in px. */
export const GIT_GRAPH_OFFSET_Y = 12
/** Commit node radius in px. */
export const GIT_GRAPH_NODE_R = 4
/** Extra vertical space in px when a commit row is expanded. */
export const GIT_GRAPH_EXPAND_Y = 250
/** Default branch colours (12) from the reference config. */
export const GIT_GRAPH_COLORS: string[] = [
  '#0085d9',
  '#d9008f',
  '#00d90a',
  '#d98500',
  '#a300d9',
  '#ff0000',
  '#00d9cc',
  '#e138e8',
  '#85d900',
  '#dc5b23',
  '#6f24d6',
  '#ffcc00',
]
/** Bezier control offset factor for the rounded graph style. */
export const GIT_GRAPH_CURVE_FACTOR_ROUNDED = 0.8
/** Bezier control offset factor for the angular graph style (reserved). */
export const GIT_GRAPH_CURVE_FACTOR_ANGULAR = 0.38

/** Curve offset `d` for the rounded style (GRID_Y * 0.8 = 19.2). */
export const GIT_GRAPH_CURVE_D = GIT_GRAPH_GRID_Y * GIT_GRAPH_CURVE_FACTOR_ROUNDED

// --- Public types ---

/** Logical graph coordinate (column / row). */
export interface GraphPoint {
  x: number
  y: number
}

/** A single logical line on a branch. */
export interface GraphSegment {
  p1: GraphPoint
  p2: GraphPoint
  /** True => transition locked to p1, false => locked to p2. */
  lockedFirst: boolean
  isCommitted: boolean
}

/** A rendered branch: colour index plus its logical segments. */
export interface GraphBranch {
  colour: number
  segments: GraphSegment[]
}

/** A placed commit vertex. */
export interface GraphVertex {
  id: number
  hash: string
  /** Placed column. */
  x: number
  /** Next free column at this row (drives width computation). */
  nextX: number
  /** Raw branch colour index (resolve via `GIT_GRAPH_COLORS[colour % len]`). */
  colour: number
  isCurrent: boolean
  isMerge: boolean
}

/** Full engine layout for a commit list. */
export interface GitGraphLayout {
  vertices: GraphVertex[]
  branches: GraphBranch[]
  /** Per-vertex colour index (already modulo the palette length). */
  vertexColours: number[]
  widthsAtVertices: number[]
  contentWidth: number
  height: number
  orderedCommits: GitCommit[]
}

export interface ComputeGitGraphLayoutOptions {
  headHash?: string | null
  expandedIndex?: number
  expandY?: number
}

/** A single SVG sub-path of a branch (split on committed/uncommitted). */
export interface GraphBranchPath {
  d: string
  isCommitted: boolean
  /** Raw branch colour index. */
  colour: number
}

// --- Internal engine classes (port of `Vertex` / `Branch`) ---

const NULL_VERTEX_ID = -1

type VertexOrNull = EngineVertex | null

interface UnavailablePoint {
  connectsTo: VertexOrNull
  onBranch: EngineBranch | null
}

class EngineBranch {
  private readonly colour: number
  private end = 0
  private lines: { p1: GraphPoint; p2: GraphPoint; lockedFirst: boolean }[] = []
  private numUncommitted = 0

  constructor(colour: number) {
    this.colour = colour
  }

  public addLine(p1: GraphPoint, p2: GraphPoint, isCommitted: boolean, lockedFirst: boolean): void {
    this.lines.push({ p1, p2, lockedFirst })
    if (isCommitted) {
      if (p2.x === 0 && p2.y < this.numUncommitted) this.numUncommitted = p2.y
    } else {
      this.numUncommitted++
    }
  }

  public getColour(): number {
    return this.colour
  }

  public getEnd(): number {
    return this.end
  }

  public setEnd(end: number): void {
    this.end = end
  }

  public getLines(): ReadonlyArray<{ p1: GraphPoint; p2: GraphPoint; lockedFirst: boolean }> {
    return this.lines
  }

  public getNumUncommitted(): number {
    return this.numUncommitted
  }
}

class EngineVertex {
  public readonly id: number

  private x = 0
  private children: EngineVertex[] = []
  private parents: EngineVertex[] = []
  private nextParent = 0
  private onBranch: EngineBranch | null = null
  private isCommitted = true
  private isCurrent = false
  private nextX = 0
  private connections: UnavailablePoint[] = []

  constructor(id: number) {
    this.id = id
  }

  public addChild(vertex: EngineVertex): void {
    this.children.push(vertex)
  }

  public addParent(vertex: EngineVertex): void {
    this.parents.push(vertex)
  }

  public getNextParent(): VertexOrNull {
    if (this.nextParent < this.parents.length) return this.parents[this.nextParent] as EngineVertex
    return null
  }

  public getLastParent(): VertexOrNull {
    if (this.nextParent < 1) return null
    return this.parents[this.nextParent - 1] as EngineVertex
  }

  public registerParentProcessed(): void {
    this.nextParent++
  }

  public isMerge(): boolean {
    return this.parents.length > 1
  }

  public addToBranch(branch: EngineBranch, x: number): void {
    if (this.onBranch === null) {
      this.onBranch = branch
      this.x = x
    }
  }

  public isNotOnBranch(): boolean {
    return this.onBranch === null
  }

  public isOnThisBranch(branch: EngineBranch): boolean {
    return this.onBranch === branch
  }

  public getBranch(): EngineBranch | null {
    return this.onBranch
  }

  public getPoint(): GraphPoint {
    return { x: this.x, y: this.id }
  }

  public getNextPoint(): GraphPoint {
    return { x: this.nextX, y: this.id }
  }

  public getNextX(): number {
    return this.nextX
  }

  public getPointConnectingTo(vertex: VertexOrNull, onBranch: EngineBranch): GraphPoint | null {
    for (let i = 0; i < this.connections.length; i++) {
      const conn = this.connections[i] as UnavailablePoint
      if (conn.connectsTo === vertex && conn.onBranch === onBranch) {
        return { x: i, y: this.id }
      }
    }
    return null
  }

  public registerUnavailablePoint(
    x: number,
    connectsToVertex: VertexOrNull,
    onBranch: EngineBranch,
  ): void {
    if (x === this.nextX) {
      this.nextX = x + 1
      this.connections[x] = { connectsTo: connectsToVertex, onBranch }
    }
  }

  public getColour(): number {
    return this.onBranch !== null ? this.onBranch.getColour() : 0
  }

  public getIsCommitted(): boolean {
    return this.isCommitted
  }

  public setNotCommitted(): void {
    this.isCommitted = false
  }

  public setCurrent(): void {
    this.isCurrent = true
  }

  public getIsCurrent(): boolean {
    return this.isCurrent
  }
}

// --- Layout computation (port of `Graph.loadCommits` + `determinePath`) ---

/**
 * Stable reorder that guarantees child-before-parent order without
 * re-sorting by timestamp: repeatedly move a commit directly before its
 * earliest parent when it appears after one. Already-ordered lists are
 * returned unchanged (same element order).
 */
export function enforceChildBeforeParent(commits: GitCommit[]): GitCommit[] {
  const ordered = [...commits]
  const position = new Map<string, number>()
  const reindex = (): void => {
    position.clear()
    for (let i = 0; i < ordered.length; i++) {
      position.set((ordered[i] as GitCommit).hash, i)
    }
  }
  reindex()
  let moved = true
  let guard = ordered.length * ordered.length + 1
  while (moved && guard-- > 0) {
    moved = false
    for (let i = 0; i < ordered.length; i++) {
      const commit = ordered[i] as GitCommit
      let earliestParent = -1
      for (const parent of commit.parents) {
        const parentIndex = position.get(parent)
        if (parentIndex !== undefined && (earliestParent === -1 || parentIndex < earliestParent)) {
          earliestParent = parentIndex
        }
      }
      if (earliestParent !== -1 && earliestParent < i) {
        ordered.splice(earliestParent, 0, ...(ordered.splice(i, 1) as GitCommit[]))
        reindex()
        moved = true
        break
      }
    }
  }
  return ordered
}

/**
 * Compute the Git Graph layout for commits in given order (newest first).
 *
 * The input order is authoritative and is never re-sorted — like the
 * reference, which relies on the order delivered by the data source.
 * Ordering guarantee: every commit must appear before its parents (child-
 * before-parent, i.e. newest first). Entries violating this (e.g. a test or
 * mock listing a commit after its parent) are moved directly before their
 * first missing parent, so the ported determinePath pass can still form
 * one continuous branch (mirrors the reference behaviour where parents
 * already sit later in the list).
 */
export function computeGitGraphLayout(
  inputCommits: GitCommit[],
  opts: ComputeGitGraphLayoutOptions = {},
): GitGraphLayout {
  const { headHash = null, expandedIndex = -1, expandY = GIT_GRAPH_EXPAND_Y } = opts

  // Enforce child-before-parent order: the determinePath port (like the
  // reference) expects every parent to sit later in the list. Bubble any
  // commit directly before its earliest parent when violated.
  const commits = enforceChildBeforeParent(inputCommits)

  const vertices: EngineVertex[] = []
  const branches: EngineBranch[] = []
  const availableColours: number[] = []
  const commitLookup: Record<string, number> = {}
  for (let i = 0; i < commits.length; i++) {
    commitLookup[commits[i]!.hash] = i
  }

  if (commits.length === 0) {
    return {
      vertices: [],
      branches: [],
      vertexColours: [],
      widthsAtVertices: [],
      contentWidth: getContentWidth([]),
      height: getGraphHeight(0, false, expandY),
      orderedCommits: [],
    }
  }

  const nullVertex = new EngineVertex(NULL_VERTEX_ID)
  for (let i = 0; i < commits.length; i++) {
    vertices.push(new EngineVertex(i))
  }
  for (let i = 0; i < commits.length; i++) {
    const commit = commits[i] as GitCommit
    for (let j = 0; j < commit.parents.length; j++) {
      const parentHash = commit.parents[j] as string
      const parentIndex = commitLookup[parentHash] as number | undefined
      const vertex = vertices[i] as EngineVertex
      if (typeof parentIndex === 'number') {
        vertex.addParent(vertices[parentIndex] as EngineVertex)
        ;(vertices[parentIndex] as EngineVertex).addChild(vertex)
      } else {
        // Parent is not part of the graph (shallow boundary).
        vertex.addParent(nullVertex)
      }
    }
  }

  if (headHash !== null && typeof commitLookup[headHash] === 'number') {
    ;(vertices[commitLookup[headHash] as number] as EngineVertex).setCurrent()
  }

  function getAvailableColour(startAt: number): number {
    for (let i = 0; i < availableColours.length; i++) {
      if (startAt > (availableColours[i] as number)) {
        return i
      }
    }
    availableColours.push(0)
    return availableColours.length - 1
  }

  function determinePath(startAt: number): void {
    let i = startAt
    let vertex = vertices[i] as EngineVertex
    let parentVertex = (vertices[i] as EngineVertex).getNextParent()
    let curVertex: EngineVertex
    const lastPointRef: { point: GraphPoint } = {
      point: vertex.isNotOnBranch() ? vertex.getNextPoint() : vertex.getPoint(),
    }
    let curPoint: GraphPoint

    if (
      parentVertex !== null &&
      parentVertex.id !== NULL_VERTEX_ID &&
      vertex.isMerge() &&
      !vertex.isNotOnBranch() &&
      !parentVertex.isNotOnBranch()
    ) {
      // Branch is a merge between two vertices already on branches
      let foundPointToParent = false
      const parentBranch = parentVertex.getBranch() as EngineBranch
      for (i = startAt + 1; i < vertices.length; i++) {
        curVertex = vertices[i] as EngineVertex
        const existing = curVertex.getPointConnectingTo(parentVertex, parentBranch)
        if (existing !== null) {
          curPoint = existing
          foundPointToParent = true
        } else {
          curPoint = curVertex.getNextPoint()
        }
        parentBranch.addLine(
          lastPointRef.point,
          curPoint,
          vertex.getIsCommitted(),
          !foundPointToParent && curVertex !== parentVertex
            ? lastPointRef.point.x < curPoint.x
            : true,
        )
        curVertex.registerUnavailablePoint(curPoint.x, parentVertex, parentBranch)
        lastPointRef.point = curPoint
        if (foundPointToParent) {
          vertex.registerParentProcessed()
          break
        }
      }
    } else {
      // Branch is normal
      const branch = new EngineBranch(getAvailableColour(startAt))
      vertex.addToBranch(branch, lastPointRef.point.x)
      vertex.registerUnavailablePoint(lastPointRef.point.x, vertex, branch)
      for (i = startAt + 1; i < vertices.length; i++) {
        curVertex = vertices[i] as EngineVertex
        curPoint =
          parentVertex === curVertex && !parentVertex.isNotOnBranch()
            ? curVertex.getPoint()
            : curVertex.getNextPoint()
        branch.addLine(
          lastPointRef.point,
          curPoint,
          vertex.getIsCommitted(),
          lastPointRef.point.x < curPoint.x,
        )
        curVertex.registerUnavailablePoint(curPoint.x, parentVertex, branch)
        lastPointRef.point = curPoint
        if (parentVertex === curVertex) {
          // The parent of <vertex> has been reached, progress <vertex> and
          // <parentVertex> to continue building the branch
          vertex.registerParentProcessed()
          const parentVertexOnBranch = !parentVertex.isNotOnBranch()
          parentVertex.addToBranch(branch, curPoint.x)
          vertex = parentVertex
          parentVertex = vertex.getNextParent()
          if (parentVertex === null || parentVertexOnBranch) {
            // There are no more parent vertices, or the parent was already on a branch
            break
          }
        }
      }
      if (i === vertices.length && parentVertex !== null && parentVertex.id === NULL_VERTEX_ID) {
        // Vertex is the last in the graph, so no more branch can be formed to the parent
        vertex.registerParentProcessed()
      }
      branch.setEnd(i)
      branches.push(branch)
      availableColours[branch.getColour()] = i
    }
  }

  let i = 0
  while (i < vertices.length) {
    const current = vertices[i] as EngineVertex
    if (current.getNextParent() !== null || current.isNotOnBranch()) {
      determinePath(i)
    } else {
      i++
    }
  }

  const exportedVertices: GraphVertex[] = vertices.map((v, index) => ({
    id: v.id,
    hash: (commits[index] as GitCommit).hash,
    x: v.getPoint().x,
    nextX: v.getNextX(),
    colour: v.getColour(),
    isCurrent: v.getIsCurrent(),
    isMerge: v.isMerge(),
  }))

  const exportedBranches: GraphBranch[] = branches.map((b) => ({
    colour: b.getColour(),
    segments: b.getLines().map((line, index) => ({
      p1: { ...line.p1 },
      p2: { ...line.p2 },
      lockedFirst: line.lockedFirst,
      isCommitted: index >= b.getNumUncommitted(),
    })),
  }))

  const expanded = expandedIndex > -1
  return {
    vertices: exportedVertices,
    branches: exportedBranches,
    vertexColours: exportedVertices.map((v) => v.colour % GIT_GRAPH_COLORS.length),
    widthsAtVertices: getWidthsAtVertices(exportedVertices),
    contentWidth: getContentWidth(exportedVertices),
    height: getGraphHeight(commits.length, expanded, expandY),
    orderedCommits: [...commits],
  }
}

// --- Pixel helpers (pure, no DOM) ---

/** Pixel centre of a vertex, stretching rows below an expanded commit. */
export function vertexPixel(
  vertex: Pick<GraphVertex, 'id' | 'x'>,
  expandedIndex = -1,
  expandY: number = GIT_GRAPH_EXPAND_Y,
): { cx: number; cy: number } {
  return {
    cx: vertex.x * GIT_GRAPH_GRID_X + GIT_GRAPH_OFFSET_X,
    cy:
      vertex.id * GIT_GRAPH_GRID_Y +
      GIT_GRAPH_OFFSET_Y +
      (expandedIndex > -1 && vertex.id > expandedIndex ? expandY : 0),
  }
}

interface PlacedPixelLine {
  p1: { x: number; y: number }
  p2: { x: number; y: number }
  isCommitted: boolean
  lockedFirst: boolean
}

/**
 * Convert a branch into SVG path strings (port of `Branch.draw`).
 * Returns one entry per contiguous committed/uncommitted run so Phase 3 can
 * render each directly as a `<path>` (colour via
 * `GIT_GRAPH_COLORS[colour % len]`).
 */
export function branchPaths(
  branch: GraphBranch,
  expandedIndex = -1,
  expandY: number = GIT_GRAPH_EXPAND_Y,
  style: 'rounded' | 'angular' = 'rounded',
): GraphBranchPath[] {
  const d = GIT_GRAPH_GRID_Y * (style === 'angular' ? GIT_GRAPH_CURVE_FACTOR_ANGULAR : GIT_GRAPH_CURVE_FACTOR_ROUNDED)
  const expandAt = expandedIndex

  // Convert branch lines into pixel coordinates, respecting expanded commit extensions
  const lines: PlacedPixelLine[] = []
  for (let idx = 0; idx < branch.segments.length; idx++) {
    const line = branch.segments[idx] as GraphSegment
    let x1 = line.p1.x * GIT_GRAPH_GRID_X + GIT_GRAPH_OFFSET_X
    let y1 = line.p1.y * GIT_GRAPH_GRID_Y + GIT_GRAPH_OFFSET_Y
    let x2 = line.p2.x * GIT_GRAPH_GRID_X + GIT_GRAPH_OFFSET_X
    let y2 = line.p2.y * GIT_GRAPH_GRID_Y + GIT_GRAPH_OFFSET_Y

    // If a commit is expanded, stretch the graph for the commit details view height
    if (expandAt > -1) {
      if (line.p1.y > expandAt) {
        // The line starts after the expansion, move the whole line lower
        y1 += expandY
        y2 += expandY
      } else if (line.p2.y > expandAt) {
        // The line crosses the expansion
        if (x1 === x2) {
          // The line is vertical, extend the endpoint past the expansion
          y2 += expandY
        } else if (line.lockedFirst) {
          // Locked to the first point, the transition stays in its normal position
          lines.push({ p1: { x: x1, y: y1 }, p2: { x: x2, y: y2 }, isCommitted: line.isCommitted, lockedFirst: line.lockedFirst })
          lines.push({ p1: { x: x2, y: y1 + GIT_GRAPH_GRID_Y }, p2: { x: x2, y: y2 + expandY }, isCommitted: line.isCommitted, lockedFirst: line.lockedFirst })
          continue
        } else {
          // Locked to the second point, the transition moves to after the expansion
          lines.push({ p1: { x: x1, y: y1 }, p2: { x: x1, y: y2 - GIT_GRAPH_GRID_Y + expandY }, isCommitted: line.isCommitted, lockedFirst: line.lockedFirst })
          y1 += expandY
          y2 += expandY
        }
      }
    }
    lines.push({ p1: { x: x1, y: y1 }, p2: { x: x2, y: y2 }, isCommitted: line.isCommitted, lockedFirst: line.lockedFirst })
  }

  // Simplify consecutive lines that are straight by removing the 'middle' point
  let s = 0
  while (s < lines.length - 1) {
    const line = lines[s] as PlacedPixelLine
    const nextLine = lines[s + 1] as PlacedPixelLine
    if (
      line.p1.x === line.p2.x &&
      line.p2.x === nextLine.p1.x &&
      nextLine.p1.x === nextLine.p2.x &&
      line.p2.y === nextLine.p1.y &&
      line.isCommitted === nextLine.isCommitted
    ) {
      line.p2.y = nextLine.p2.y
      lines.splice(s + 1, 1)
    } else {
      s++
    }
  }

  // Iterate through all lines, producing the svg paths
  const paths: GraphBranchPath[] = []
  let curPath = ''
  let curCommitted = true
  for (let k = 0; k < lines.length; k++) {
    const line = lines[k] as PlacedPixelLine
    const prev = k > 0 ? (lines[k - 1] as PlacedPixelLine) : null
    const x1 = line.p1.x
    const y1 = line.p1.y
    const x2 = line.p2.x
    const y2 = line.p2.y

    // If the new point belongs to a different path, store the current path and reset it
    if (curPath !== '' && prev !== null && line.isCommitted !== prev.isCommitted) {
      paths.push({ d: curPath, isCommitted: prev.isCommitted, colour: branch.colour })
      curPath = ''
    }

    // If the path hasn't been started or the new point belongs to a different path, move to p1
    if (curPath === '' || (prev !== null && (x1 !== prev.p2.x || y1 !== prev.p2.y))) {
      curPath += 'M' + x1.toFixed(0) + ',' + y1.toFixed(1)
      curCommitted = line.isCommitted
    }

    if (x1 === x2) {
      // Vertical path, draw a straight line
      curPath += 'L' + x2.toFixed(0) + ',' + y2.toFixed(1)
    } else if (style === 'angular') {
      // Horizontal transition with straight segments
      curPath +=
        'L' +
        (line.lockedFirst
          ? x2.toFixed(0) + ',' + (y2 - d).toFixed(1)
          : x1.toFixed(0) + ',' + (y1 + d).toFixed(1)) +
        'L' +
        x2.toFixed(0) +
        ',' +
        y2.toFixed(1)
    } else {
      // Rounded transition with a bezier curve
      curPath +=
        'C' +
        x1.toFixed(0) +
        ',' +
        (y1 + d).toFixed(1) +
        ' ' +
        x2.toFixed(0) +
        ',' +
        (y2 - d).toFixed(1) +
        ' ' +
        x2.toFixed(0) +
        ',' +
        y2.toFixed(1)
    }
    curCommitted = line.isCommitted
  }

  if (curPath !== '') {
    paths.push({ d: curPath, isCommitted: curCommitted, colour: branch.colour })
  }
  return paths
}

/** Total graph height in px (port of `Graph.getHeight`). */
export function getGraphHeight(count: number, expanded: boolean, expandY: number = GIT_GRAPH_EXPAND_Y): number {
  return count * GIT_GRAPH_GRID_Y + GIT_GRAPH_OFFSET_Y - GIT_GRAPH_GRID_Y / 2 + (expanded ? expandY : 0)
}

/** Per-row graph widths in px (port of `Graph.getWidthsAtVertices`). */
export function getWidthsAtVertices(vertices: Pick<GraphVertex, 'nextX'>[]): number[] {
  return vertices.map((v) => GIT_GRAPH_OFFSET_X + v.nextX * GIT_GRAPH_GRID_X - 2)
}

/** Graph content width in px (port of `Graph.getContentWidth`). */
export function getContentWidth(vertices: Pick<GraphVertex, 'nextX'>[]): number {
  let x = 0
  for (const v of vertices) {
    if (v.nextX > x) x = v.nextX
  }
  return 2 * GIT_GRAPH_OFFSET_X + (x - 1) * GIT_GRAPH_GRID_X
}

// ---------------------------------------------------------------------------
// Legacy lane API (deprecated) — kept for GitGraphSection.vue + store until
// Phase 3 migrates the component to the engine above.
// ---------------------------------------------------------------------------

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

export interface LegacyGitGraphLayout {
  rows: GitGraphRow[]
  edges: GitGraphEdge[]
  laneCount: number
}

/**
 * @deprecated Use {@link computeGitGraphLayout} instead. Kept as a
 * compatibility wrapper so `GitGraphSection.vue` and the store keep working
 * until Phase 3 migrates them to the new engine.
 *
 * Compute row/lane layout for a commit list.
 *
 * Commits are sorted newest first by timestamp; mock data and locally
 * created commits guarantee parents are always older than their children.
 */
export function computeGraphLayout(commits: GitCommit[]): LegacyGitGraphLayout {
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
