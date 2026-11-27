/**
 * Geometry of the sign-in stage: OpenCuria's hexagonal mark and the lattice of
 * further cubes that tiles the plane around it.
 */

type Point = readonly [number, number];

// The mark's hexagon in its own units, around its center (brand SVGs: 32,32).
const TOP: Point = [0, -17];
const UPPER_RIGHT: Point = [14, -9];
const LOWER_RIGHT: Point = [14, 9];
const BOTTOM: Point = [0, 17];
const LOWER_LEFT: Point = [-14, 9];
const UPPER_LEFT: Point = [-14, -9];
const CENTER: Point = [0, 0];

/** The hexagons tile the plane along these steps: a column and a row. */
const COLUMN_STEP = 28;
const ROW_STEP: Point = [14, 26];

/** Rings of neighbors drawn around the mark; further ones would be invisible. */
export const LATTICE_RINGS = 5;

/** Which part of the sign-in lights a facet: see `LoginStage`. */
export type FacetSide = "top" | "left" | "right";

/** The mark's six facets in the order the webapp's working chase lights them. */
export const FACETS = [
  { side: "top", points: [UPPER_LEFT, TOP, CENTER], fill: "#D6D2F2" },
  { side: "top", points: [TOP, UPPER_RIGHT, CENTER], fill: "#FFFFFF" },
  {
    side: "right",
    points: [UPPER_RIGHT, CENTER, LOWER_RIGHT],
    fill: "url(#oc-r)",
  },
  { side: "right", points: [CENTER, BOTTOM, LOWER_RIGHT], fill: "url(#oc-br)" },
  { side: "left", points: [CENTER, BOTTOM, LOWER_LEFT], fill: "#6A6399" },
  { side: "left", points: [UPPER_LEFT, CENTER, LOWER_LEFT], fill: "#9B95C9" },
] as const satisfies readonly {
  side: FacetSide;
  points: readonly Point[];
  fill: string;
}[];

export interface LatticeCell {
  /** Center relative to the mark, in mark units. */
  x: number;
  y: number;
  /** Hexagonal distance from the mark: 1 for its six neighbors. */
  ring: number;
}

/**
 * The cubes that tile a `width` × `height` area (mark units) centered on the
 * mark, excluding the mark itself and anything beyond {@link LATTICE_RINGS}.
 */
export function latticeCells(width: number, height: number): LatticeCell[] {
  const cells: LatticeCell[] = [];
  const rows = Math.ceil(height / 2 / ROW_STEP[1]) + 1;
  for (let row = -rows; row <= rows; row++) {
    const y = row * ROW_STEP[1];
    if (Math.abs(y) - BOTTOM[1] > height / 2) continue;
    const reach = Math.ceil((width / 2 + COLUMN_STEP) / COLUMN_STEP) + rows;
    for (let column = -reach; column <= reach; column++) {
      const x = column * COLUMN_STEP + row * ROW_STEP[0];
      if (Math.abs(x) - UPPER_RIGHT[0] > width / 2) continue;
      // Axial coordinates: the six neighbors are (±1, 0), (0, ±1), ±(1, -1).
      const ring =
        (Math.abs(column) + Math.abs(row) + Math.abs(column + row)) / 2;
      if (ring === 0 || ring > LATTICE_RINGS) continue;
      cells.push({ x, y, ring });
    }
  }
  return cells;
}

/** Resting opacity of a ring: the lattice fades out away from the mark. */
export function ringOpacity(ring: number): number {
  return 0.75 * Math.pow(1 - (ring - 1) / LATTICE_RINGS, 2.2);
}

/** SVG path data of one or more cubes, by face. */
export interface CubePaths {
  top: string;
  left: string;
  right: string;
  /** Outline and the three ridges that meet in the front corner. */
  edges: string;
}

export interface StagePaths {
  /** The lattice by ring, nearest first. */
  rings: CubePaths[];
  /** The mark before its facets light. */
  mark: CubePaths;
  /** The mark's facets, in {@link FACETS} order. */
  facets: string[];
}

/**
 * Path data for a `width` × `height` stage (points) with the mark centered,
 * drawn `unit` points per mark unit.
 */
export function stagePaths(
  width: number,
  height: number,
  unit: number,
): StagePaths {
  const cx = width / 2;
  const cy = height / 2;
  const rings = Array.from({ length: LATTICE_RINGS }, emptyPaths);
  for (const cell of latticeCells(width / unit, height / unit)) {
    addCube(rings[cell.ring - 1], cx + cell.x * unit, cy + cell.y * unit, unit);
  }
  const mark = emptyPaths();
  addCube(mark, cx, cy, unit);
  const at = pointAt(cx, cy, unit);
  const facets = FACETS.map((facet) => `M${facet.points.map(at).join("L")}Z`);
  return { rings, mark, facets };
}

function emptyPaths(): CubePaths {
  return { top: "", left: "", right: "", edges: "" };
}

function pointAt(x: number, y: number, unit: number) {
  return ([px, py]: Point) =>
    `${(x + px * unit).toFixed(1)} ${(y + py * unit).toFixed(1)}`;
}

function addCube(paths: CubePaths, x: number, y: number, unit: number): void {
  const at = pointAt(x, y, unit);
  const shape = (points: readonly Point[]) => `M${points.map(at).join("L")}Z`;
  paths.top += shape([UPPER_LEFT, TOP, UPPER_RIGHT, CENTER]);
  paths.left += shape([UPPER_LEFT, CENTER, BOTTOM, LOWER_LEFT]);
  paths.right += shape([CENTER, UPPER_RIGHT, LOWER_RIGHT, BOTTOM]);
  paths.edges +=
    shape([UPPER_LEFT, TOP, UPPER_RIGHT, LOWER_RIGHT, BOTTOM, LOWER_LEFT]) +
    `M${at(CENTER)}L${at(UPPER_LEFT)}` +
    `M${at(CENTER)}L${at(UPPER_RIGHT)}` +
    `M${at(CENTER)}L${at(BOTTOM)}`;
}
