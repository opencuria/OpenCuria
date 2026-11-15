import { describe, expect, it, vi } from "vitest";

import {
  filterMentionCandidates,
  MENTION_FILE_LIMIT,
  workspaceRelativePath,
} from "../src/harnessMentions";

describe("file mention ranking", () => {
  it("retains the best matches even when they arrive after the file cap", () => {
    const paths = [
      ...Array.from({ length: 20 }, (_, i) => `/workspace/util/z-${i}.ts`),
      "/workspace/src/z-util.ts",
      "/workspace/src/a-util.ts",
      "/workspace/src/utility.ts",
      "/workspace/src/util.ts",
      "/workspace/unrelated.ts",
    ];
    const original = [...paths];

    expect(filterMentionCandidates("FILE:UTIL", paths)).toEqual(
      [
        "src/util.ts",
        "src/utility.ts",
        "src/a-util.ts",
        "src/z-util.ts",
        "util/z-0.ts",
        "util/z-1.ts",
        "util/z-10.ts",
        "util/z-11.ts",
      ].map((label) => ({
        kind: "file",
        label,
        insert: `file:/workspace/${label}`,
      })),
    );
    expect(paths).toEqual(original);
  });

  it.each(["", "file:", "file:alpha"])(
    "keeps input order for equal labels and scores (%s)",
    (query) => {
      const paths = Array.from({ length: 20 }, (_, i) =>
        i % 2 ? "alpha.ts" : "/workspace/alpha.ts",
      );
      const agents = query ? [] : filterMentionCandidates("", []);
      const files = paths
        .slice(0, query ? MENTION_FILE_LIMIT : 6)
        .map((path) => ({
          kind: "file",
          label: "alpha.ts",
          insert: `file:${path}`,
        }));
      expect(filterMentionCandidates(query, paths)).toEqual([
        ...agents,
        ...files,
      ]);
    },
  );

  it("uses locale ordering for case, accents and punctuation within each score", () => {
    const paths = [
      "Éclair.ts",
      "école.ts",
      "Alpha.ts",
      "alpha.ts",
      "a-b.ts",
      "a_b.ts",
    ].map((name) => `/workspace/${name}`);
    expect(
      filterMentionCandidates("file:", paths).map((row) => row.label),
    ).toEqual(
      paths.map(workspaceRelativePath).sort((a, b) => a.localeCompare(b)),
    );
    expect(
      filterMentionCandidates("file:É", paths).map((row) => row.label),
    ).toEqual(["Éclair.ts", "école.ts"].sort((a, b) => a.localeCompare(b)));
  });

  it("matches full paths and leaves agent-only and missing queries unchanged", () => {
    const paths = ["/workspace/a.ts", "/elsewhere/b.ts"];
    expect(filterMentionCandidates("file:workspace", paths)).toEqual([
      { kind: "file", label: "a.ts", insert: "file:/workspace/a.ts" },
    ]);
    expect(filterMentionCandidates("agent:pl", paths)).toEqual([
      { kind: "agent", label: "@agent:plan", insert: "agent:plan" },
      { kind: "agent", label: "@agent:explore", insert: "agent:explore" },
    ]);
    expect(filterMentionCandidates("file:missing", paths)).toEqual([]);
    expect(filterMentionCandidates("file:", [])).toEqual([]);
  });

  it("bounds ranking work for a large loaded explorer without a timing threshold", () => {
    const paths = Array.from(
      { length: 10_000 },
      (_, i) => `/workspace/project-${(i * 7919) % 10_000}/util-${i}.ts`,
    );
    const compare = vi.spyOn(String.prototype, "localeCompare");
    const normalize = vi.spyOn(String.prototype, "toLowerCase");
    try {
      const result = filterMentionCandidates("file:util", paths);
      expect(result).toHaveLength(MENTION_FILE_LIMIT);
      expect(compare.mock.calls.length).toBeLessThanOrEqual(
        paths.length * MENTION_FILE_LIMIT,
      );
      expect(normalize.mock.calls.length).toBeLessThanOrEqual(
        paths.length + 10,
      );
    } finally {
      compare.mockRestore();
      normalize.mockRestore();
    }
  });
});
