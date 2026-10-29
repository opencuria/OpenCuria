# Bounded tool-output truncation

## Investigation and selection

Started from fetched `origin/dev`, `19389bf12caa4ce6a9b198b518e1e7746926c6a2`,
on 2026-10-29. The working tree was clean; `git stash push --include-untracked`
reported nothing to save. Local and remote branches were checked before code
inspection. Existing request-lifecycle, web-resource, mobile-copy, history-read,
file-transfer and workspace-read optimizations were already merged.

The survey covered backend services/repositories/tool execution, runner file/Git/
metrics paths, web rendering/stores, mobile chat/controller and shared packages.
The following ranking weighs impact, frequency, measured confidence, complexity,
regression risk and architectural fit. Scout timings are local synthetic probes,
not production measurements.

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Runner repeated base64 normalization | Actual `files:download` handler with a synthetic service and recording emit sink: 476.53 ms median at 10 MiB raw, 4,694.47 ms at 100 MiB, seven samples each. `_emit_chunked_result` normalizes once, then normalizes again for the chunk decision and again for splitting. Large transfers block other RPC work; transport framing must remain exact. |
| 2 | Backend tool-output truncation | A 10.5 MB ASCII log needs 31.35 ms median and 45,175,421 B peak temporary allocations to produce a roughly 42 KB preview. Entire-output splitting/encoding is repeated after tool execution and during history replay. Local helper change, no persistence/protocol changes. |
| 3 | Shared streaming reducer scans | Actual reducer on Vue-reactive messages: 0.097/0.446/1.776 ms median per text event at 100/500/2,000 parts (400 measured events after 100 warmups). Stream closing and part lookup scan the growing turn repeatedly. High-frequency path, but changes risk boundary/order semantics shared by web/mobile. |
| 4 | Mobile expanded tool groups | Existing native stress evidence in `mobile-streaming-performance.md` records backlog/disconnection when expanding 2,000 tool rows. `WorkedGroup` still mounts every expanded row; promising but requires native interaction and layout verification. |
| 5 | Runner mention-file search sorting | `build_find_files_command` sorts all matching paths before `head`, despite returning at most 50. Large-tree cost is plausible but not measured here; preserving depth/alphabetical ordering complicates early termination. |
| 6 | Backend conversation-feed overfetch | Root-session reads still load summary/engine state unused by the feed. These are normally small; meaningful current-workload impact is unconfirmed. |

An actual `secrets.choice` draw over ranks 1–3, using OS randomness, selected
**rank 2, backend tool-output truncation**. No redraw occurred. Selection and scout
values are recorded in `benchmarks/tool-truncation/selection.json`.

## Change and behavior

`backend/apps/harness/tools/truncate.py` now counts newline delimiters without
creating a string per line. It splits only the first/last `max_bytes + 1`
characters: a UTF-8 character needs at least one byte, so the extra character
ensures a partial boundary line exceeds the byte budget just as the full line
would. Existing whole-line selection and oversized-single-line clipping remain
the only implementations of head/tail behavior.

ASCII byte counts use string length. Non-ASCII counts encode bounded 64 KiB
character slices, preserving exact byte markers without an entire-output copy.
The full input is still scanned to count omitted lines/bytes. For the default
positive limits, temporary helper allocations are bounded by the preview and
counting windows. Existing nonpositive-limit behavior remains unchanged.

This helper serves the existing tool registry and provider-history service.
MCP and webfetch can supply multi-megabyte text before registry clipping. Shell
output already has a separate formatter; this change does not claim to optimize
that formatter. Typical persisted tool results are already clipped, so large
history-replay gains are not assumed.

Only this helper changes in production. There is no cache, mutable shared state,
dependency, extra executor/thread hop or altered async scheduling. Existing
validation, permissions, before/after hooks, full-output spills, failure handling
and ordered replay keep their owners and contracts. REST/Socket.IO/MCP schemas
are unchanged; the runner and presentation layers are unchanged.

## Measurement

```bash
backend/.venv/bin/python scripts/benchmark_tool_truncation.py \
  --output docs/benchmarks/tool-truncation/before.json
# Run the preceding command before changing the production helper.
backend/.venv/bin/python scripts/benchmark_tool_truncation.py \
  --baseline-ref 19389bf12caa4ce6a9b198b518e1e7746926c6a2 \
  --output docs/benchmarks/tool-truncation/paired.json
```

The benchmark uses real `McpTool`, MCP result normalization, JSON-schema
validation, `ToolRegistry.execute` and spill encoding. A synthetic connection
supplies documents; a sink checks the exact full recovery bytes without retaining
each copy. Fixtures cover short text, approximately 72 KB/1.10 MB/4.42 MB ASCII
documents, a 4.03 MB Unicode document and a 4 MiB single line, in both directions.
Fixture names are approximate size labels; JSON records exact byte/line counts.

Python 3.12.3 on this Linux workspace; 5 warmups and 31 samples per case.
Paired runs alternate baseline/current order against the same fixture in one
process; the old helper is loaded from Git without changing the working tree.
p50 is the median; p95 uses the nearest rank. Allocation tracing runs separately
from latency samples. Inputs/expected bytes are prepared before timing/tracing.

The following results are from `paired-repeat.json`, repeating the same workload
after the initial comparison. Both paired runs show the same improvement.

| Head preview workload | p50 ms, before → after | p95 ms, before → after | Peak Python allocated bytes, before → after |
| --- | --- | --- | --- |
| 20 B short result | 0.324 → 0.323 | 0.352 → 0.359 | 12,606 → 12,653 |
| 71,999 B ASCII document | 0.950 → 0.953 | 1.012 → 1.024 | 338,069 → 274,855 |
| 1,103,999 B ASCII document | 3.562 → 1.774 | 4.878 → 2.386 | 4,451,282 → 1,160,651 |
| 4,415,999 B ASCII document | 13.429 → 3.720 | 17.622 → 4.889 | 17,826,770 → 4,472,605 |
| 4,031,999 B Unicode document | 24.434 → 10.426 | 26.786 → 11.406 | 32,397,124 → 11,674,783 |
| 4,194,304 B single line | 3.355 → 2.620 | 3.924 → 2.756 | 4,254,112 → 4,254,065 |

The large ASCII document improves **72.3% p50, 72.3% p95 and 74.9% peak
allocations**. Its tail case improves 73.6% p50 and 74.9% peak allocations.
The Unicode head case improves 57.3% p50 and 64.0% peak allocations. Short-input
timings are effectively unchanged. Single-line dispatch peak allocations remain
dominated by the required full-output spill encoding, despite lower helper cost.

All paired preview hashes match, including exact truncation markers; each spill
matches the original full bytes. These are CPU/allocation integration results,
not production latency or RSS. Network, MCP transport, provider latency and spill
disk/network time are excluded. No frontend/device performance claim is made.

## Verification

- Before implementation: 48 truncation/history/compaction tests passed; no
  pre-existing failure in those suites. The runner scout's 37 chunking tests
  also passed.
- New allocation regression tests failed on the original helper in all six
  ASCII/Unicode/single-line × head/tail cases and pass after optimization.
  They assert less than 1 MiB peak helper allocations on multi-megabyte inputs,
  without fragile wall-clock thresholds.
- Broader post-change suite: **339 passed** across all `test_tools_*.py`,
  `test_mcp*.py`, history loading/compaction and adapter/session boundaries.
  Existing spill success/failure and tool-hook tests pass.
- Final truncation suite after three more boundary/error cases: **42 passed**.
  Coverage includes exact limits, trailing/empty lines, CRLF, UTF-8 partial
  characters, counting-window boundaries, marker precedence, nonpositive limits,
  invalid direction and encoding errors outside the retained preview.
- An additional seeded differential probe compared the original/current helper
  on **30,056 cases**: random text/limits/directions and large newline/Unicode
  boundary fixtures; every content string and truncation flag matched.
- Ruff lint/format checks and `git diff --check` passed for changed code.
  The two existing Django constraint deprecation warnings remain.
- No UI code changed; native/browser checks are not applicable. The entire
  backend suite and production network/provider calls were not run.
