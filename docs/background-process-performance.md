# Background process observation performance

Measured on 2026-10-24 against baseline `2227194` (latest `origin/dev` at the
start). The working tree was clean; existing stashes were preserved. All local
and remote branches were checked, including recent Git-discovery, conversation
feed and harness-history performance work, to avoid repeating those changes.

## Investigation and selection

These candidates were ranked by likely latency/resource savings, execution
frequency, strength of evidence, implementation risk and architectural cost.
Only the selected candidate was changed.

| Rank | Component / path | Evidence and likely benefit | Risk / architectural cost |
| --- | --- | --- | --- |
| 1 | Runner process list and heartbeat (`runner/src/services/sessions/background.py`, `workspace_registry.py`) | One sequential runtime exec per tracked PID, plus one per stopped process. Heartbeats run every 15 seconds; process-list RPC serves the backend, UI and agent/MCP callers. Real-shell baseline confirmed 50–75 calls for 50 entries. | Low: bounded batching can reuse the existing status resolver and exit-file parser, with no state cache or concurrency. |
| 2 | Backend plugin catalog (`backend/apps/plugins/services.py:PluginService._serialize`) | Query inspection found three relation reads and one activation query per plugin, in addition to the catalog query: `1 + 4N`. REST and MCP share this service. | Low/medium: bulk relation loading must retain organization visibility, activation scope and nested ordering. |
| 3 | Mobile streaming (`mobile/src/state/chatController.ts:prepareEventMessages`) | Each delta clones the active message and every existing part, including input/meta/display objects, before reducing one part. Cost grows with tool-heavy turns; immutable snapshots must remain intact. | Medium: selective copying belongs with shared reducers and needs careful native rendering/identity verification. |
| 4 | Web streaming Markdown (`webapp/src/components/chat/HarnessMarkdown.vue`) | Every rendered revision parses/sanitizes the whole accumulated document and builds a DOM. Existing 50 ms throttling already reduces frequency. | Higher: incremental parsing must preserve reference links, sanitization, media and streaming behavior; requires running UI profiling. |
| 5 | Runner file mentions (`runner/src/services/files.py:build_find_files_command`) | `find` feeds a full depth sort before `head`, even for a 50-result response. Large trees still require traversal and sorting. | Medium: limiting traversal early can change depth ordering or completeness. |
| 6 | Shared descendant traversal (`packages/chat-core/src/harnessSubtaskActivity.ts:collectDescendantSessionIds`) | Repeated full-array passes depend on tree depth and input order. Used by both clients. | Low complexity, but bounded subagent depth limits typical impact; weaker expected benefit than the shortlist. |

The top three formed the random selection pool, in this order:

```python
secrets.choice([
    "Backend plugin catalog: four per-plugin database reads",
    "Runner process listing: sequential per-process runtime probes",
    "Mobile chat streaming: cloning every part of the active message per delta",
])
```

The actual draw selected **runner process listing**. No redraw was needed.
Other candidates have source/query inspection evidence, not measured speedups.

## Change and boundaries

The background process manager now checks up to 100 PIDs per shell invocation.
The shell uses only `kill -0` and `printf` builtins; PIDs are converted to integers
and supplied as positional arguments. Each PID produces exactly one boolean
line, validated before associating any observations with processes.

Single-process checks retain their original probe. Stopped processes still use
the existing exit-file reader and status resolver, including missing/malformed
file behavior. For `N` tracked processes and `D` stopped processes, runtime calls
fall from `N + D` to `ceil(N / 100) + D` (zero for an empty list).

Both process-list RPCs and heartbeat projection use the same batching method,
wired through the existing runner composition root. The registry snapshots
tracked entries before awaiting probes. Status ordering and metadata are retained;
observations never replace process tracking. No tasks, caches, dependencies,
backend business logic, provider rules or frontend code were added.

Interactive errors still propagate to the existing RPC error handler.
Heartbeats return `unknown` for unavailable observations and continue with other
batches. A failed batch affects only its entries; a failed exit-file read affects
only that entry. Cancellation propagates through both modes.

## Measurement

Reproduce from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_background_status.py
```

The script creates real local live and reaped processes and synthetic exit files
in a temporary directory. It calls the real Socket.IO process-list handler
(including JSON encoding) and the real heartbeat projection, using the same
runtime command interface as Docker/QEMU. Children and temporary state are
cleaned up. Each case has one warm-up and 15 timed samples; p95 uses nearest rank.
Runtime calls and Python allocation peaks are measured separately from timing.
Fixtures, interpreter and machine were the same before/after.

Undelayed local results (milliseconds):

| Workload / path | Calls before → after | p50 before → after | p95 before → after |
| --- | --- | --- | --- |
| 10 running / process-list RPC | 10 → 1 | 54.649 → 1.321 | 80.371 → 1.457 |
| 10 running / heartbeat | 10 → 1 | 46.095 → 1.252 | 50.413 → 1.422 |
| 50 running / process-list RPC | 50 → 1 | 312.834 → 5.331 | 331.167 → 5.952 |
| 50 running / heartbeat | 50 → 1 | 303.279 → 2.398 | 565.716 → 2.503 |
| 25 running + 25 exited / process-list RPC | 75 → 26 | 256.966 → 25.254 | 362.484 → 61.678 |
| 25 running + 25 exited / heartbeat | 75 → 26 | 343.396 → 24.442 | 365.439 → 30.591 |

For 50 running processes, RPC p50 fell **98.3%** and runtime calls fell **98%**.
Peak Python allocations for that RPC fell from 308,292 to 284,657 bytes; heartbeat
allocations fell from 297,154 to 279,761 bytes. These are allocation peaks, not
process RSS or guest memory. No new persistent state is retained.

The benchmark also adds an explicitly **simulated** 5 ms delay per runtime call.
For 50 running processes, RPC p50/p95 changed from 663.403/1429.213 to
7.414/10.861 ms; for the mixed case, from 1029.886/1677.003 to
186.015/263.212 ms. This demonstrates round-trip sensitivity, not measured SSH
or Docker latency.

Normalized ordered status hashes match for all 24 before/after combinations
(three process counts, two workloads, two delays, two paths). The one-process
case keeps one runtime call. A separate 51-sample repeat showed single-process
RPC medians around 5–6 ms on both versions; there is no claimed speedup there.
Shared-host scheduling and shell startup introduce noise, particularly in p95.
The deterministic call-count reduction is the performance regression guard.

Raw results for this run are in
`/workspace/reports/runner-process-performance-20261024/` (`baseline.json`,
`optimized.json`, and the two `single-*.json` repeats).

Docker and QEMU services were unavailable in this workspace, so live container/
VM transport latency and guest CPU/RSS were not measured. No UI behavior changed.

## Verification

Before production edits: 68 background/capture-fence tests and 12 registry
lifecycle tests passed. The plugin candidate's existing 59 tests also passed.

After the change:

- 93 focused runner tests passed, including 13 new real-shell cases for status
  parity, ordering/metadata, zero/single/100/101/205 entries, malformed/truncated
  output, batch/read failure isolation, cancellation/admission cleanup, shell
  metacharacters in file paths, and replacement during an in-flight probe.
- 93 backend process-service, REST, MCP and capture-interaction tests passed.
- Full runner suite: **854 passed, 7 skipped, 4 failed**, plus 33 passing subtests.
  All four failures are the parametrized
  `test_actual_backend_codex_argv_passes_production_stream_boundary` assertion
  that the Codex feature config contains `"multi_agent" = false`. The identical
  four failures were reproduced on a disposable untouched `2227194` archive;
  they are pre-existing and outside this change. Optional QEMU dependencies
  account for skipped coverage.
- Ruff checks pass on the changed background/registry code, new tests/helper
  and benchmark using the project's Python style rules. Imports/undefined-name
  checks pass on the composition root. Its unrelated pre-existing long docstring
  at `runner/src/service.py:270` still triggers E501 under a full-file check.
- `git diff --check` passes. No reference repositories or existing stashes were
  modified. Frontend checks are unnecessary for this runner-only change.
