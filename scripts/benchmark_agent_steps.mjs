#!/usr/bin/env node
/** Compare Agent-S timeline projection on plain and Vue-reactive transcripts.
 * Run from the repository root with:
 * node --expose-gc scripts/benchmark_agent_steps.mjs --baseline-ref 30b1542
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomInt } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { performance } from "node:perf_hooks";
import { parseArgs } from "node:util";
import ts from "typescript";
import { reactive } from "vue";

const { values } = parseArgs({
  options: {
    "baseline-ref": { type: "string" },
    samples: { type: "string", default: "31" },
    warmups: { type: "string", default: "5" },
    steps: { type: "string", default: "1,10,100,250,500" },
    output: { type: "string" },
  },
});
const samples = Number(values.samples);
const warmups = Number(values.warmups);
const sizes = values.steps.split(",").map(Number);
assert(Number.isInteger(samples) && samples > 0);
assert(Number.isInteger(warmups) && warmups >= 0);
assert(sizes.every((size) => Number.isInteger(size) && size > 0));
const repo = new URL("../", import.meta.url);
const sourcePath = "packages/chat-core/src/harnessAgentSteps.ts";

async function load(source, name) {
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(
    `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}#${name}`
  );
}

const versions = {
  optimized: await load(
    readFileSync(new URL(sourcePath, repo), "utf8"),
    "optimized",
  ),
};
if (values["baseline-ref"]) {
  versions.baseline = await load(
    execFileSync("git", ["show", `${values["baseline-ref"]}:${sourcePath}`], {
      cwd: repo,
      encoding: "utf8",
    }),
    "baseline",
  );
}

function fixture(count) {
  return Array.from({ length: count }, (_, step) =>
    [
      "step-start",
      "reasoning",
      "agent",
      "tool",
      ...(step < count - 1 ? ["step-finish"] : []),
    ].map((type) => ({
      id: `${type}-${step}`,
      message_id: "message",
      session_id: "session",
      type,
      state:
        type === "reasoning" && step === count - 1 ? "running" : "completed",
      title: "",
      output: type === "reasoning" ? "Checking the next page." : "",
      meta: {
        step,
        ...(type === "agent"
          ? {
              agent_meta: {
                verification: "Previous page checked",
                analysis: `Page ${step} is ready`,
                action: "click Next page",
                action_kind: "click",
              },
            }
          : {}),
      },
    })),
  ).flat();
}

function project(module, parts) {
  return {
    views: module.buildAgentStepViews(parts, { streaming: true }),
    running: module.hasRunningAgentSequence(parts),
  };
}

function summary(times) {
  const ordered = [...times].sort((a, b) => a - b);
  return {
    p50_ms: ordered[Math.floor(ordered.length / 2)],
    p95_ms: ordered[Math.ceil(ordered.length * 0.95) - 1],
    samples_ms: times,
  };
}

function countReads(module, parts) {
  let reads = 0;
  const observed = parts.map(
    (part) =>
      new Proxy(part, {
        get(target, key, receiver) {
          if (key === "type") reads += 1;
          return Reflect.get(target, key, receiver);
        },
      }),
  );
  project(module, observed);
  return reads;
}

const results = [];
for (const steps of sizes) {
  for (const mode of ["plain", "vue-reactive"]) {
    const plain = fixture(steps);
    const parts = mode === "plain" ? plain : reactive(plain);
    const expected = project(versions.optimized, parts);
    const digest = createHash("sha256")
      .update(JSON.stringify(expected))
      .digest("hex");
    for (const module of Object.values(versions)) {
      assert.deepEqual(project(module, parts), expected, "Projection changed");
    }
    const iterations = Math.max(1, Math.floor(100 / steps));
    const measurements = Object.fromEntries(
      Object.keys(versions).map((key) => [key, []]),
    );
    for (let sample = -warmups; sample < samples; sample++) {
      const order = Object.keys(versions);
      if (randomInt(2)) order.reverse();
      for (const key of order) {
        const start = performance.now();
        for (let iteration = 0; iteration < iterations; iteration++) {
          project(versions[key], parts);
        }
        const duration = (performance.now() - start) / iterations;
        if (sample >= 0) measurements[key].push(duration);
      }
    }
    const versionsResult = {};
    for (const [key, times] of Object.entries(measurements)) {
      assert.deepEqual(project(versions[key], parts), expected);
      versionsResult[key] = {
        ...summary(times),
      };
    }
    results.push({
      steps,
      parts: parts.length,
      mode,
      response_sha256: digest,
      ...versionsResult,
    });
    process.stderr.write(`Measured ${steps} steps (${mode})\n`);
  }
}

// Instrument only after every latency sample: proxy counters can change V8's
// type feedback even when the counting call itself is outside the timed region.
for (const result of results) {
  const parts = fixture(result.steps);
  for (const [key, module] of Object.entries(versions)) {
    result[key].part_type_reads = countReads(module, parts);
  }
}

// A separate retained-heap probe detects accidental persistent caching. This is
// noisy process heap after GC, not peak allocation or browser/device RSS.
const retainedHeap = {};
if (global.gc) {
  const parts = fixture(Math.max(...sizes));
  for (const [name, module] of Object.entries(versions)) {
    project(module, parts);
    global.gc();
    const before = process.memoryUsage().heapUsed;
    for (let iteration = 0; iteration < 20; iteration++) project(module, parts);
    global.gc();
    retainedHeap[name] = process.memoryUsage().heapUsed - before;
  }
}
const report = {
  node: process.version,
  baseline_ref: values["baseline-ref"] ?? null,
  samples,
  warmups,
  method:
    "Randomized paired order on identical inputs; complete step views and running-sequence detection. Small cases batched per sample.",
  limitation:
    "Local synthetic projections; Vue proxies included, DOM/network/provider latency excluded. See separate running-app measurements.",
  retained_heap_growth_after_gc_bytes: retainedHeap,
  results,
};
const output = `${JSON.stringify(report, null, 2)}\n`;
if (values.output) writeFileSync(values.output, output);
else process.stdout.write(output);
