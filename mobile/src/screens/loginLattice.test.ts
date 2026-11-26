import { describe, expect, it } from "vitest";
import {
  FACETS,
  LATTICE_RINGS,
  latticeCells,
  ringOpacity,
  stagePaths,
} from "./loginLattice";

describe("latticeCells", () => {
  it("surrounds the mark with hexagonal rings of 6, 12, 18 cubes", () => {
    const cells = latticeCells(1000, 1000);
    const count = (ring: number) =>
      cells.filter((cell) => cell.ring === ring).length;
    expect(cells.some((cell) => cell.x === 0 && cell.y === 0)).toBe(false);
    expect([count(1), count(2), count(3)]).toEqual([6, 12, 18]);
    expect(Math.max(...cells.map((cell) => cell.ring))).toBe(LATTICE_RINGS);
  });

  it("places the first ring edge to edge with the mark", () => {
    const neighbors = latticeCells(1000, 1000)
      .filter((cell) => cell.ring === 1)
      .map(({ x, y }) => `${x},${y}`)
      .sort();
    expect(neighbors).toEqual(
      ["-14,-26", "-14,26", "-28,0", "14,-26", "14,26", "28,0"].sort(),
    );
  });

  it("keeps only cubes that reach into the area", () => {
    for (const cell of latticeCells(100, 60)) {
      expect(Math.abs(cell.x) - 14).toBeLessThanOrEqual(50);
      expect(Math.abs(cell.y) - 17).toBeLessThanOrEqual(30);
    }
  });
});

describe("stagePaths", () => {
  it("draws the mark centered with one path per facet", () => {
    const paths = stagePaths(200, 300, 2);
    expect(paths.facets).toHaveLength(FACETS.length);
    // The front corner of the mark is the stage's center.
    expect(paths.facets[0]).toContain("L100.0 150.0Z");
    expect(paths.rings).toHaveLength(LATTICE_RINGS);
    expect(paths.rings[0].edges).not.toBe("");
  });

  it("lights each side of the mark from one part of the sign-in", () => {
    const sides = FACETS.map((facet) => facet.side);
    expect(sides.filter((side) => side === "top")).toHaveLength(2);
    expect(sides.filter((side) => side === "left")).toHaveLength(2);
    expect(sides.filter((side) => side === "right")).toHaveLength(2);
  });
});

it("fades the lattice out away from the mark", () => {
  const opacities = Array.from({ length: LATTICE_RINGS }, (_, index) =>
    ringOpacity(index + 1),
  );
  expect(opacities).toEqual([...opacities].sort((a, b) => b - a));
  expect(opacities[LATTICE_RINGS - 1]).toBeLessThan(0.05);
});
