#!/usr/bin/env node
/** Measure the actual mobile controller with synthetic REST and realtime inputs.
 * Run: node --expose-gc scripts/benchmark_mobile_stream.mjs --output report.json
 * Rendering/network/provider time is excluded. No native-device claims are made.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { build } from "esbuild";

const { values } = parseArgs({
  options: {
    samples: { type: "string", default: "31" },
    chunks: { type: "string", default: "100" },
    parts: { type: "string", default: "100,500,2000" },
    "baseline-ref": { type: "string" },
    output: { type: "string" },
  },
});
const samples = Number(values.samples);
const chunks = Number(values.chunks);
const sizes = values.parts.split(",").map(Number);
const STREAM_TEXT = "Streaming the next code review finding. ";
assert(Number.isInteger(samples) && samples >= 2);
assert(Number.isInteger(chunks) && chunks > 0);
assert(sizes.every((size) => Number.isInteger(size) && size > 0));

const repo = fileURLToPath(new URL("../", import.meta.url));
const bundle = await build({
  absWorkingDir: repo,
  entryPoints: ["mobile/src/state/chatController.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
  plugins: values["baseline-ref"]
    ? [
        {
          name: "baseline-source",
          setup(builder) {
            builder.onLoad({ filter: /\.ts$/ }, ({ path }) => {
              const sourcePath = relative(repo, path);
              if (!/^(mobile|packages)\//.test(sourcePath)) return;
              return {
                contents: execFileSync(
                  "git",
                  ["show", `${values["baseline-ref"]}:${sourcePath}`],
                  {
                    cwd: repo,
                    encoding: "utf8",
                  },
                ),
                loader: "ts",
              };
            });
          },
        },
      ]
    : [],
});
const directory = mkdtempSync(join(tmpdir(), "opencuria-stream-bench-"));
let ChatController;
try {
  const modulePath = join(directory, "controller.mjs");
  writeFileSync(modulePath, bundle.outputFiles[0].text);
  ({ ChatController } = await import(pathToFileURL(modulePath).href));
} finally {
  rmSync(directory, { recursive: true, force: true });
}

function fixture(count) {
  const session = {
    id: "root",
    workspace_id: "workspace",
    parent_id: null,
    harness_id: "native",
    title: "Synthetic code review",
    mode: "build",
    agent_name: "build",
    model: "test/model",
    reasoning_effort: "medium",
    status: "busy",
    cost: 0,
    tokens: {},
    unread: false,
    manual_unread: false,
  };
  const messages = Array.from({ length: 50 }, (_, i) => ({
    id: `history-${i}`,
    session_id: "root",
    role: i % 2 ? "assistant" : "user",
    position: i,
    content: "Earlier conversation",
    parts: [],
    completed_at: "2026-01-01T00:00:00Z",
  }));
  messages.push({
    id: "live",
    session_id: "root",
    role: "assistant",
    position: 50,
    content: "",
    parts: Array.from({ length: count }, (_, i) => ({
      id: `part-${i}`,
      message_id: "live",
      session_id: "root",
      position: i,
      type: i === count - 1 ? "text" : "tool",
      state: i === count - 1 ? "running" : "completed",
      title: "Read source",
      output: "",
      input: { tool: "read" },
      meta: { step: i },
      display: {
        summary: "Read source",
        preview: [{ kind: "text", text: "Result" }],
      },
      detail_loaded: false,
    })),
  });
  return { session, messages, permissions: [], questions: [] };
}

async function setup(count) {
  const timeline = fixture(count);
  const handlers = new Map();
  const realtime = {
    isConnected: true,
    on(event, callback) {
      handlers.set(event, callback);
      return () => handlers.delete(event);
    },
    onReconnect() {
      return () => {};
    },
    subscribeWorkspace() {
      return () => {};
    },
  };
  const client = {
    realtime,
    api: { get: async () => [] },
    harness: {
      listSessions: async () => [timeline.session],
      timeline: async () => timeline,
      markRead: async () => {},
    },
  };
  const controller = new ChatController(client);
  await controller.openWorkspace("workspace", "root");
  assert.equal(controller.getSnapshot().messages.at(-1).parts.length, count);
  return {
    controller,
    emit: (event, payload) => handlers.get(event)?.(payload),
  };
}

function stream(run, count, total) {
  let notifications = 0;
  const unsubscribe = run.controller.subscribe(() => notifications++);
  const before = run.controller.getSnapshot();
  const priorMessage = before.messages.at(-1);
  const durations = [];
  for (let i = 0; i < total; i++) {
    const start = performance.now();
    run.emit("harness.part_updated", {
      workspace_id: "workspace",
      session_id: "root",
      message_id: "live",
      part_id: `part-${count - 1}`,
      part_position: count - 1,
      delta: { text: STREAM_TEXT },
    });
    durations.push(performance.now() - start);
  }
  unsubscribe();
  const after = run.controller.getSnapshot();
  const nextMessage = after.messages.at(-1);
  assert.equal(notifications, total);
  assert.equal(priorMessage.content, "");
  assert.equal(nextMessage.content, STREAM_TEXT.repeat(total));
  assert.equal(before.messages[0], after.messages[0]);
  assert(Object.isFrozen(nextMessage.parts));
  return {
    durations,
    replaced_parts: nextMessage.parts.filter(
      (part, i) => part !== priorMessage.parts[i],
    ).length,
    digest: createHash("sha256")
      .update(JSON.stringify(after.messages))
      .digest("hex"),
  };
}

const results = [];
for (const count of sizes) {
  const elapsed = [];
  let measured;
  for (let sample = 0; sample < samples + 3; sample++) {
    const run = await setup(count);
    global.gc?.();
    const result = stream(run, count, chunks);
    if (sample >= 3) {
      elapsed.push(...result.durations);
      measured = result;
    }
    run.controller.dispose();
  }
  elapsed.sort((a, b) => a - b);
  results.push({
    parts: count,
    p50_ms_per_event: elapsed[Math.floor(elapsed.length / 2)],
    p95_ms_per_event: elapsed[Math.ceil(elapsed.length * 0.95) - 1],
    replaced_parts: measured.replaced_parts,
    transcript_sha256: measured.digest,
  });
}
const report = {
  benchmark: "Mobile ChatController REST hydration and ordered realtime stream",
  node: process.version,
  source: values["baseline-ref"] ?? "working-tree",
  samples,
  chunks_per_sample: chunks,
  history_messages: 50,
  notes: [
    "Three warmups per size; fresh frozen transcript per sample.",
    "Timing includes reducer, snapshot publication and a synchronous subscriber.",
    "Network, native rendering and provider latency excluded; desktop CPU.",
  ],
  results,
};
if (values.output)
  writeFileSync(values.output, JSON.stringify(report, null, 2) + "\n");
process.stdout.write(JSON.stringify(report, null, 2) + "\n");
