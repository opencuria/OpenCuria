import {
  GIT_GRAPH_COLOR_COUNT,
  GIT_GRAPH_CURVE_FACTOR_ROUNDED,
  type GitGraphLayout,
} from "@opencuria/chat-core/gitGraph";

export interface GraphGeometry {
  /** Row height in points; one commit per row. */
  rowHeight: number;
  /** Horizontal lane step in points. */
  gridX: number;
  /** Left inset of lane 0's centre. */
  offsetX: number;
}

export interface GraphRowPath {
  d: string;
  colour: number;
}

export interface GraphRow {
  paths: GraphRowPath[];
  node: { cx: number; colour: number; current: boolean } | null;
}

type Point = { x: number; y: number };

const fmt = (value: number) => Number(value.toFixed(1));

function lerp(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function cubic(points: [Point, Point, Point, Point], shiftY: number): string {
  const [p0, p1, p2, p3] = points.map((p) => ({ x: p.x, y: p.y - shiftY }));
  return `M${fmt(p0!.x)},${fmt(p0!.y)}C${fmt(p1!.x)},${fmt(p1!.y)} ${fmt(p2!.x)},${fmt(p2!.y)} ${fmt(p3!.x)},${fmt(p3!.y)}`;
}

/** Lanes the graph needs (widest row) — sizes the graph column. */
export function graphLaneCount(layout: GitGraphLayout): number {
  let lanes = 1;
  for (const vertex of layout.vertices) lanes = Math.max(lanes, vertex.nextX);
  return lanes;
}

/** Column width for `lanes` lanes at the given geometry. */
export function graphColumnWidth(
  lanes: number,
  geometry: GraphGeometry,
): number {
  return 2 * geometry.offsetX + Math.max(0, lanes - 1) * geometry.gridX;
}

/**
 * Split the shared vscode-git-graph layout into per-row drawings, so a
 * virtualized native list can draw each commit row on its own. Every lane
 * segment joins two adjacent rows; it is cut at their boundary — straight
 * lanes into two halves, rounded transitions by splitting the same cubic
 * Bézier the webapp draws at t = ½ (its midpoint lies on the boundary).
 */
export function buildGraphRows(
  layout: GitGraphLayout,
  geometry: GraphGeometry,
): GraphRow[] {
  const { rowHeight: h, gridX, offsetX } = geometry;
  const curve = h * GIT_GRAPH_CURVE_FACTOR_ROUNDED;
  const rows: GraphRow[] = layout.vertices.map(() => ({
    paths: [],
    node: null,
  }));
  const px = (x: number) => x * gridX + offsetX;
  const py = (y: number) => y * h + h / 2;

  for (const branch of layout.branches) {
    const colour = branch.colour % GIT_GRAPH_COLOR_COUNT;
    for (const segment of branch.segments) {
      const top = Math.min(segment.p1.y, segment.p2.y);
      const upper = segment.p1.y <= segment.p2.y ? segment.p1 : segment.p2;
      const lower = upper === segment.p1 ? segment.p2 : segment.p1;
      if (lower.y - upper.y !== 1) continue;
      const a = { x: px(upper.x), y: py(upper.y) };
      const b = { x: px(lower.x), y: py(lower.y) };
      const boundary = (top + 1) * h;
      if (a.x === b.x) {
        rows[top]?.paths.push({
          d: `M${fmt(a.x)},${fmt(h / 2)}L${fmt(a.x)},${fmt(h)}`,
          colour,
        });
        rows[top + 1]?.paths.push({
          d: `M${fmt(b.x)},0L${fmt(b.x)},${fmt(h / 2)}`,
          colour,
        });
        continue;
      }
      const p1 = { x: a.x, y: a.y + curve };
      const p2 = { x: b.x, y: b.y - curve };
      const m01 = lerp(a, p1);
      const m12 = lerp(p1, p2);
      const m23 = lerp(p2, b);
      const m012 = lerp(m01, m12);
      const m123 = lerp(m12, m23);
      const mid = { x: (m012.x + m123.x) / 2, y: boundary };
      rows[top]?.paths.push({ d: cubic([a, m01, m012, mid], top * h), colour });
      rows[top + 1]?.paths.push({
        d: cubic([mid, m123, m23, b], (top + 1) * h),
        colour,
      });
    }
  }
  layout.vertices.forEach((vertex, index) => {
    const row = rows[vertex.id] ?? rows[index];
    if (row)
      row.node = {
        cx: px(vertex.x),
        colour: vertex.colour % GIT_GRAPH_COLOR_COUNT,
        current: vertex.isCurrent,
      };
  });
  return rows;
}
