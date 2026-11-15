import { describe, expect, it } from "vitest";
import { buildLineDiff, exceedsDiffLimit } from "./lineDiff";

const lines = (count: number, prefix = "line") =>
  Array.from({ length: count }, (_, index) => `${prefix} ${index + 1}`).join(
    "\n",
  ) + "\n";

describe("buildLineDiff", () => {
  it("reports identical files without rows", () => {
    const diff = buildLineDiff("a\n", "a\n");
    expect(diff.identical).toBe(true);
    expect(diff.rows).toEqual([]);
  });

  it("numbers a new file from line 1 without a phantom gap", () => {
    const diff = buildLineDiff("", "one\ntwo\n");
    expect(diff.additions).toBe(2);
    expect(diff.rows.map((row) => [row.kind, row.newNo, row.text])).toEqual([
      ["add", 1, "one"],
      ["add", 2, "two"],
    ]);
  });

  it("collapses unchanged lines around a change", () => {
    const original = lines(40);
    const modified = original.replace("line 20\n", "line twenty\n");
    const diff = buildLineDiff(original, modified);
    expect(diff.additions).toBe(1);
    expect(diff.deletions).toBe(1);
    const kinds = diff.rows.map((row) => row.kind);
    expect(kinds[0]).toBe("gap");
    expect(diff.rows[0]!.hidden).toBe(16);
    expect(kinds.at(-1)).toBe("gap");
    expect(diff.rows.at(-1)!.hidden).toBe(17);
    const deleted = diff.rows.find((row) => row.kind === "del")!;
    const added = diff.rows.find((row) => row.kind === "add")!;
    expect([deleted.oldNo, deleted.text]).toEqual([20, "line 20"]);
    expect([added.newNo, added.text]).toEqual([20, "line twenty"]);
  });

  it("counts deletions of a removed file", () => {
    const diff = buildLineDiff("a\nb\n", "");
    expect(diff.deletions).toBe(2);
    expect(diff.rows.every((row) => row.kind === "del")).toBe(true);
  });

  it("guards very large files", () => {
    expect(exceedsDiffLimit(lines(20_001), "")).toBe(true);
    expect(exceedsDiffLimit(lines(10), lines(10))).toBe(false);
  });
});
