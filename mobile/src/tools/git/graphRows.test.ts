import { describe, expect, it } from "vitest";
import { computeGitGraphLayout } from "@opencuria/chat-core/gitGraph";
import type { GitCommit } from "@opencuria/chat-core/gitTypes";
import { buildGraphRows, graphColumnWidth, graphLaneCount } from "./graphRows";

const commit = (hash: string, parents: string[]): GitCommit => ({
  hash,
  message: hash,
  author: "a",
  timestamp: "2026-01-01T00:00:00Z",
  parents,
});

const geometry = { rowHeight: 40, gridX: 14, offsetX: 14 };

describe("buildGraphRows", () => {
  it("draws a linear history as straight half-lanes and one node per row", () => {
    const layout = computeGitGraphLayout(
      [commit("c", ["b"]), commit("b", ["a"]), commit("a", [])],
      { headHash: "c" },
    );
    const rows = buildGraphRows(layout, geometry);
    expect(rows).toHaveLength(3);
    expect(rows[0]!.node).toEqual({ cx: 14, colour: 0, current: true });
    expect(rows[0]!.paths.map((p) => p.d)).toEqual(["M14,20L14,40"]);
    expect(rows[1]!.paths.map((p) => p.d).sort()).toEqual([
      "M14,0L14,20",
      "M14,20L14,40",
    ]);
    expect(rows[2]!.paths.map((p) => p.d)).toEqual(["M14,0L14,20"]);
  });

  it("splits a lane transition at the row boundary", () => {
    // m merges side branch s into main; s forks from a.
    const layout = computeGitGraphLayout([
      commit("m", ["b", "s"]),
      commit("s", ["a"]),
      commit("b", ["a"]),
      commit("a", []),
    ]);
    const rows = buildGraphRows(layout, geometry);
    expect(graphLaneCount(layout)).toBe(2);
    expect(graphColumnWidth(2, geometry)).toBe(42);
    const curves = rows
      .flatMap((row) => row.paths)
      .filter((p) => p.d.includes("C"));
    expect(curves.length).toBeGreaterThan(0);
    for (const path of curves) {
      const numbers = path.d.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
      const ys = numbers.filter((_, index) => index % 2 === 1);
      expect(Math.min(...ys)).toBeGreaterThanOrEqual(0);
      expect(Math.max(...ys)).toBeLessThanOrEqual(40);
    }
  });
});
