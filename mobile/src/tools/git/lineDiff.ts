import { structuredPatch } from "diff";

export type DiffRowKind = "context" | "add" | "del" | "gap";

/** One rendered row of a unified diff. */
export interface DiffRow {
  key: string;
  kind: DiffRowKind;
  oldNo: number | null;
  newNo: number | null;
  /** Line text; for `gap` rows the number of hidden unchanged lines. */
  text: string;
  hidden?: number;
}

export interface LineDiff {
  rows: DiffRow[];
  additions: number;
  deletions: number;
  /** Neither side has content (e.g. both missing). */
  empty: boolean;
  /** Same content on both sides. */
  identical: boolean;
}

/** Unchanged lines kept around each change. */
const CONTEXT = 3;
/** Above this many lines per side the diff is not computed on the device. */
export const MAX_DIFF_LINES = 20_000;

function lineCount(text: string): number {
  if (!text) return 0;
  let count = 1;
  for (let at = text.indexOf("\n"); at !== -1; at = text.indexOf("\n", at + 1))
    count += 1;
  return text.endsWith("\n") ? count - 1 : count;
}

/**
 * Unified line diff of two file versions with collapsed unchanged regions
 * between hunks, like the webapp's inline diff (additions/deletions counted
 * per line).
 */
export function buildLineDiff(original: string, modified: string): LineDiff {
  if (original === modified)
    return {
      rows: [],
      additions: 0,
      deletions: 0,
      empty: !original,
      identical: true,
    };
  const patch = structuredPatch("a", "b", original, modified, "", "", {
    context: CONTEXT,
  });
  const rows: DiffRow[] = [];
  let additions = 0;
  let deletions = 0;
  // jsdiff numbers a pure insertion/deletion by the line that follows it.
  let previousOldLast = 0;
  const totalOld = lineCount(original);
  for (const [index, hunk] of patch.hunks.entries()) {
    const firstOld = hunk.oldStart;
    const hiddenBefore = firstOld - (previousOldLast + 1);
    if (hiddenBefore > 0)
      rows.push({
        key: `gap-${index}`,
        kind: "gap",
        oldNo: null,
        newNo: null,
        text: "",
        hidden: hiddenBefore,
      });
    let oldNo = firstOld;
    let newNo = hunk.newStart;
    for (const [lineIndex, line] of hunk.lines.entries()) {
      const marker = line[0];
      const text = line.slice(1);
      const key = `${index}-${lineIndex}`;
      if (marker === "\\") continue; // "\ No newline at end of file"
      if (marker === "+") {
        additions += 1;
        rows.push({ key, kind: "add", oldNo: null, newNo: newNo++, text });
      } else if (marker === "-") {
        deletions += 1;
        rows.push({ key, kind: "del", oldNo: oldNo++, newNo: null, text });
      } else {
        rows.push({
          key,
          kind: "context",
          oldNo: oldNo++,
          newNo: newNo++,
          text,
        });
      }
    }
    previousOldLast = hunk.oldStart + hunk.oldLines - 1;
  }
  const hiddenAfter = totalOld - previousOldLast;
  if (patch.hunks.length && hiddenAfter > 0)
    rows.push({
      key: "gap-end",
      kind: "gap",
      oldNo: null,
      newNo: null,
      text: "",
      hidden: hiddenAfter,
    });
  return { rows, additions, deletions, empty: false, identical: false };
}

/** True when either side is too large to diff on the device. */
export function exceedsDiffLimit(original: string, modified: string): boolean {
  return (
    lineCount(original) > MAX_DIFF_LINES || lineCount(modified) > MAX_DIFF_LINES
  );
}
