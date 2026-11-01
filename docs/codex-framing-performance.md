# Codex fragmented NDJSON: avoid rescanning buffered records

Baseline: `3ca2251a22e2908d9f03a1f74d7f4843bfd3ea6f`, fetched from `origin/dev`.
Both AGENTS.md files were read. The working tree was clean; no stash was needed.
Local and remote branches were checked, including unmerged branches. Existing
performance branches had no commits ahead of dev covering this work.

## Investigation and selection

The survey covered backend repositories and engine streams, runner metrics,
files and inventory, web chat stores and Markdown, mobile chat rendering, and
the shared reducer/client. These opportunities were ranked by likely impact,
frequency, confidence, complexity, regression risk and architectural fit:

| Rank | Candidate | Evidence and impact / risk |
| --- | --- | --- |
| 1 | Backend Claude NDJSON framing | Repeatedly searches and UTF-8 encodes the growing partial record. Actual adapter scout: 0.42 / 14.79 / 223.67 ms for 64 KiB / 1 MiB / 4 MiB payloads in 4 KiB chunks. Relevant to large tool/message records; localized transport change with UTF-8/error-boundary tests needed. |
| 2 | Backend Codex text accumulation | Concatenates and encodes the full accumulated item on every delta. Actual notification-handler scout: 55.16 / 1,283.35 / 13,875.82 ms for those sizes in 128-character deltas. Includes redaction/event projection, so these costs cannot all be attributed to accumulation. Frequent streaming path; changes must preserve redaction and completed-snapshot reconciliation. |
| 3 | Backend Codex NDJSON framing | Searches the growing bytearray from zero after every read. Actual reader scout: 0.36 / 7.28 / 74.03 ms for those sizes in 4 KiB chunks. Large tool results and completed-item snapshots delay backend dispatch; a cursor can eliminate rescans with very little semantic risk. |
| 4 | Mobile chat list | `ChatMessages.tsx` maps history inside a ScrollView; rows are not virtualized. Potentially high long-chat cost, but native-device profiling and scroll/gesture work exceed this bounded transport change. |
| 5 | Web Markdown parsing | `HarnessMarkdown.vue` parses and sanitizes the complete document. Render coalescing already exists; further benefit needs browser profiling and must preserve whole-document references. |
| 6 | Shared streaming reducer | `harnessReducer.ts` scans parts for lookup and stream closure on deltas. Current immutable updates already share completed parts; indexing adds state/invalidation risk. |
| 7 | Backend credential-removal checks | `PluginService.credential_removal_gaps` queries requirements per effective plugin. Confirmed query shape, but an infrequent destructive-action path compared with streaming. |
| 8 | Runner image inventory | `QemuRuntime.inventory` verifies published image digests while scanning inventory. Potential disk/CPU cost on large images; integrity proofs make skipping or caching this work risky without a broader investigation. |

The first three form the shortlist. An actual OS-backed
`secrets.choice(['claude_ndjson', 'codex_text', 'codex_ndjson'])` draw selected
**Codex NDJSON framing**. No redraw was needed. Scout results and the draw are in
`benchmarks/codex-framing/investigation.json` and `selection.json`. Scout timings
are five-sample medians and include fixture setup; the dedicated benchmark below
is the basis for the before/after claims.

## Change

`backend/apps/harness/engines/codex/protocol.py:CodexProtocol._read` now remembers
how many retained bytes were already searched for LF. The next read starts its
search there. Dispatching a complete record removes it and resets the cursor.
Searching a fragmented N-byte record previously cost O(N²/chunk_size); it now
searches each byte once. Parsing and notification serialization are unchanged.

The implementation adds one reader-local integer, no cache or task. Buffer
compaction still happens before dispatch, preserving its original memory
lifetime. An initial experimental version delayed compaction and increased peak
allocations; that part was removed before the recorded final measurements.

The reader remains in the backend engine transport. The runner stays a dumb
byte executor, and REST/Socket.IO/MCP and frontend contracts are untouched.
Frame/chunk limits, strict UTF-8, blank-line handling, EOF/error behavior, RPC
correlation, callback scheduling, queue accounting and cancellation are unchanged.

## Dedicated measurement

From the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_codex_framing.py --baseline-ref 3ca2251a22e2908d9f03a1f74d7f4843bfd3ea6f > before.json
backend/.venv/bin/python scripts/benchmark_codex_framing.py > after.json
```

The script runs the actual reader, strict JSON decoding, validation, dispatcher,
notification serialization and queue accounting. Synthetic wire chunks are built
before timing; each receive yields once. Each workload is warmed, then measured
31 times on the same host/Python environment. CPU time accompanies wall time;
tracemalloc and an asyncio heartbeat run separately. No database, provider,
network or QEMU VM is involved. These are transport-processing costs, not
end-to-end production latency or SLOs. Both 4 KiB fragments and the stream's
64 KiB write size are covered, plus multibyte UTF-8 and coalesced small deltas.

| Payload / chunks | Before p50 / p95 ms | After p50 / p95 ms | p50 improvement |
| --- | --- | --- | --- |
| 64 KiB / 4 KiB | 0.366 / 0.429 | 0.354 / 0.484 | Essentially unchanged |
| 1 MiB / 4 KiB | 7.764 / 8.701 | 4.836 / 6.385 | 37.7% |
| 4 MiB / 4 KiB | 71.074 / 80.800 | 20.178 / 21.733 | 71.6% (3.52×) |
| 12 MiB / 4 KiB | 886.003 / 937.326 | 69.107 / 71.933 | 92.2% (12.82×) |
| 12 MiB / 64 KiB | 99.047 / 116.914 | 46.883 / 58.132 | 52.7% (2.11×) |
| 1.02 MB UTF-8 / 4,093 bytes | 8.437 / 8.589 | 5.441 / 5.749 | 35.5% |
| 256 coalesced small deltas / 64 KiB | 1.753 / 1.882 | 1.695 / 1.778 | Essentially unchanged |

At 12 MiB / 4 KiB, median CPU falls from 885.899 to 69.073 ms. Peak traced
allocations remain 53,486,503 vs 53,486,511 bytes: the cursor adds eight bytes in
this run. All ten workloads have identical before/after result hashes. Small
record timings vary by fractions of a millisecond; no meaningful improvement is
claimed there. The maximum heartbeat gap does not improve reliably: synchronous
JSON parsing/serialization of the completed frame still dominates that gap.

Raw metrics, source hashes and Python version are in
`benchmarks/codex-framing/{before,after}.json`. The benchmark can load the original
protocol directly from Git without checking out or changing the worktree.

## Verification

- Before the change, 26 relevant existing tests passed with two existing Django
  constraint deprecation warnings. No failures were found.
- Twelve new framing/behavior cases passed on the baseline. The new deterministic
  performance test failed as intended: 131,182 wire bytes caused 67,305,582 bytes
  of newline search. The optimized implementation keeps search work bounded by
  wire length, without a timing threshold.
- Final focused checks: 24 passed. Coverage includes split UTF-8, multiple records
  and partial tails in a chunk, CRLF/blank lines, exact and oversized frames,
  malformed input, EOF, queue ceilings, RPC ordering and cancellation.
- Broader checks: `test_codex_core.py`, `test_codex_core_safety.py`,
  `test_codex_limits.py`, `test_codex_framing.py` and
  `test_codex_service_runtime.py`: 52 passed, 41 skipped. Skips require the absent
  pinned Codex executable; no live-provider/runtime validation is claimed.
- Ruff lint/format and `git diff --check` pass. No UI code changes, dependency
  changes, new persistent state, or architecture boundary changes.

The highest-potential follow-ups are Claude NDJSON framing, Codex text
accumulation, and mobile history virtualization. None is changed here.
