#!/usr/bin/env node
/** Compare the shared mention pipeline with a Git baseline, without network/UI.
 * Run: node scripts/benchmark_mention_ranking.mjs --baseline-ref c5d57cb
 * Includes explorer flattening and search-result merging, as used by Vue.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { transform } from "esbuild";

const { values } = parseArgs({
  options: {
    "baseline-ref": { type: "string", default: "c5d57cb" },
    samples: { type: "string", default: "31" },
    output: { type: "string" },
  },
});
const samples = Number(values.samples);
assert(Number.isInteger(samples) && samples >= 2);
const repo = fileURLToPath(new URL("../", import.meta.url));
const path = "packages/chat-core/src/harnessMentions.ts";
const sources = [
  execFileSync("git", ["show", `${values["baseline-ref"]}:${path}`], {
    cwd: repo,
    encoding: "utf8",
  }),
  readFileSync(new URL(`../${path}`, import.meta.url), "utf8"),
];
const modules = await Promise.all(
  sources.map(async (source) => {
    const { code } = await transform(source, { loader: "ts", format: "esm" });
    return import(
      `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`
    );
  }),
);

// Compare many queries and input orders to the original stable sort. Include
// collation ties, duplicate paths, Unicode and paths outside /workspace.
let seed = 20261030;
function random() {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 2 ** 32;
}
for (let trial = 0; trial < 100; trial++) {
  const names = [
    "util.ts",
    "utility.ts",
    "notes-util.md",
    "README",
    "Éclair.ts",
    "école.ts",
    "İ.ts",
    "a_b.ts",
  ];
  const paths = Array.from({ length: 100 }, () => {
    const root = ["/workspace/", "/workspace/src/", "/elsewhere/", ""][
      Math.floor(random() * 4)
    ];
    return root + names[Math.floor(random() * names.length)];
  });
  for (const query of [
    "",
    "file:",
    "util",
    "FILE:UTIL",
    "file:src",
    "file:workspace",
    "file:É",
    "file:i",
    "agent:pl",
    "missing",
  ]) {
    assert.deepEqual(
      modules[1].filterMentionCandidates(query, paths),
      modules[0].filterMentionCandidates(query, paths),
    );
  }
}

function percentile(sorted, fraction) {
  return sorted[Math.ceil(sorted.length * fraction) - 1];
}
const measurements = [];
for (const size of [50, 1000, 10_000]) {
  const tree = Array.from({ length: size }, (_, i) => ({
    name: `util-${i}.ts`,
    path: `/workspace/packages/project-${(i * 7919) % size}/src/util-${i}.ts`,
    type: "file",
    size: 100,
  }));
  for (const query of [
    "file:util",
    "file:",
    "file:util-9",
    "file:missing",
    "agent:pl",
  ]) {
    const execute = (index) => {
      const api = modules[index];
      return api.filterMentionCandidates(
        query,
        api.mergeMentionFilePaths([], api.flattenFilePaths(tree)),
      );
    };
    for (let i = 0; i < 5; i++) assert.deepEqual(execute(0), execute(1));
    const timings = [[], []];
    for (let i = 0; i < samples; i++) {
      for (const index of random() < 0.5 ? [0, 1] : [1, 0]) {
        const start = performance.now();
        execute(index);
        timings[index].push(performance.now() - start);
      }
    }
    measurements.push({
      files: size,
      query,
      ...Object.fromEntries(
        timings.map((times, index) => {
          times.sort((a, b) => a - b);
          return [
            index ? "optimized" : "baseline",
            {
              p50_ms: Number(percentile(times, 0.5).toFixed(3)),
              p95_ms: Number(percentile(times, 0.95).toFixed(3)),
            },
          ];
        }),
      ),
    });
  }
}
const report = {
  node: process.version,
  baseline_ref: values["baseline-ref"],
  samples,
  warmups: 5,
  differential_cases: 1000,
  measurements,
};
const output = JSON.stringify(report, null, 2) + "\n";
if (values.output) writeFileSync(values.output, output);
process.stdout.write(output);
