# Harness exec output decoding

Started from `origin/dev` at `dbfd15cdbad786385540c543d15fc39fcbe1687c`.
Read both AGENTS files, checked local/remote branches, attempted an
include-untracked stash (the checkout was clean), fetched and reset `dev`
before inspecting implementation code. The existing performance branches were
already merged. Reference repositories and internal runner state were untouched.

## Investigation and random selection

The survey covered backend services/repositories, runner command/file execution,
Vue chat rendering, native chat publication and both shared client packages.
Ranking considered effect on latency/responsiveness, frequency, confidence,
complexity, regression risk and architectural ownership:

| Rank | Opportunity | Evidence and importance | Risk / architectural fit |
| --- | --- | --- | --- |
| 1 | Runner harness exec frame decoding | Actual parser, two 5 MiB GNU-base64-wrapped streams: 74.321 ms p50 / 86.351 ms p95; 43.60 MiB peak Python allocations beyond the existing input. Synchronous parsing blocks all runner event handling after command completion. | High confidence; localized transport change. Preserve malformed frames, Unicode whitespace and decoding errors. |
| 2 | Backend pending question reads | Actual repository SQL against disposable SQLite with 50,001 retained rows: 7.731 ms p50 for one session, 16.123 ms for 50; both plans scan the whole question table and sort. Timeline and conversation attention reads pay this even when almost everything is answered. | High confidence; an appropriate index could preserve existing repository/service boundaries. Migration and write overhead need verification. |
| 3 | Backend tool-output truncation | Actual helper, 10 MiB build log: 21.086 ms p50 and 31.06 MiB peak; 10 MiB of short lines: 57.691 ms / 51.99 MiB. Splits/encodes the entire output to retain a small preview; used after tools and in provider history. | High confidence; bounded-window processing is plausible. Exact UTF-8 clipping, omission counts and tail behavior need care. |
| 4 | Runner file autocomplete | `build_find_files_command` traverses and sorts every matching file before keeping at most 51 rows. Interactive file mentions exercise it. | Source evidence only; early stopping can break shallow-first/lexical ordering. Needs realistic filesystem profiling. |
| 5 | Mobile snapshot publication | `ChatController.buildSnapshot` copies/freezes sessions, todos and gates on each accepted stream event. Previous transcript structural-sharing improvements are already present. | Source evidence only; handset profiling needed. Preserve immutable snapshots and subscription behavior. |
| 6 | Web streamed Markdown | `HarnessMarkdown.parseMarkdownRoot` parses, sanitizes and traverses the complete growing document, already coalesced into 50 ms windows. | Source evidence only; security, reference links and media semantics increase risk. |
| 7 | Shared descendant traversal | `collectDescendantSessionIds` repeatedly scans sessions; mobile snapshot publication calls it. | Source evidence only; bounded depth reduces likely normal-workload value. Keep shared client rules centralized. |

The top three were fixed before a single OS-random `secrets.randbelow(3)` draw
returned **0**, selecting runner decoding. No redraw occurred. Scout values and
the timestamped draw are recorded in
[harness-exec-selection.json](benchmarks/harness-exec-selection.json).
Current shared busy-transcript reconciliation already uses maps/sets; an older
quadratic-lookup observation was excluded after checking the current code.
Only runner decoding was changed.

## Implementation and boundaries

`runner/src/services/harness_exec.py::_parse_harness_exec_output` previously
split the full frame repeatedly, split base64 into a list of wrapped lines,
joined those lines and encoded another ASCII byte buffer before decoding.

The parser now finds frame boundaries by index and slices each encoded stream
only when decoding it. Standard-library `binascii.a2b_base64` accepts the ASCII
string directly and skips wrapped whitespace in native code. A non-ASCII frame
retains the original Unicode-whitespace normalization and validation behavior.
The first stdout/stderr/exit marker semantics, raw-output fallback, permissive
base64, UTF-8 replacement and exception types/messages are preserved.

This is one implementation inside the existing executor service. There is no
business/provider/permission logic in the runner, new dependency, cache, retained
state, worker, lock, await point or protocol change. The shell wrapper, credential
sourcing, workdir validation, capture admission, exit evidence and Socket.IO
adapter remain on their existing paths. Parsing remains synchronous and linear;
this removes redundant work rather than introducing a scheduling mechanism.

## Measurement

Run from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_harness_exec.py \
  --baseline-ref dbfd15cdbad786385540c543d15fc39fcbe1687c \
  --samples 41 --output docs/benchmarks/harness-exec.json
```

The benchmark loads the original service from Git without changing the checkout.
Both services use identical temporary log files, real sh/mktemp/cat/GNU-base64
processes and the public `exec_harness_command` path. Decoder measurements use
an actual frame captured from that wrapper. Commands return exit code 7 with
independent stdout/stderr; complete result equality is checked after every call.
Four concurrent medium commands additionally exercise independent responses.

Each workload has three warmups and 41 pairs in seeded shuffled order. Fixture
creation, equality checks and allocation tracing are outside timing. p95 uses
nearest rank. A separate `tracemalloc` probe measures peak additional Python
allocations, excluding fixtures/existing input and excluding child-process memory;
it is not RSS. A 1 ms asyncio heartbeat records the maximum scheduling gap for
each call/batch. Raw samples and stream SHA-256 hashes are in
[harness-exec.json](benchmarks/harness-exec.json).

Before production edits, a 21-pair integration run measured 78.801 ms decoder
p50 and 138.048 ms complete-command p50 for the largest fixture. A 31-pair
post-change run measured 137.436 → 94.087 ms complete-command p50. The final
41-pair run below ran separately from test processes on Python 3.12.3/Linux.

| Bytes per stream / simultaneous commands | Decoder p50 before → after | Decoder p95 before → after | Complete command p50 before → after | Complete command p95 before → after |
| --- | --- | --- | --- | --- |
| 4 KiB / 1 | 0.061 → 0.034 ms | 0.197 → 0.109 ms | 49.510 → 41.668 ms | 65.843 → 65.783 ms |
| 256 KiB / 1 | 2.985 → 1.704 ms | 3.250 → 1.831 ms | 38.228 → 37.286 ms | 78.479 → 66.565 ms |
| 5 MiB / 1 | 73.132 → 40.124 ms | 81.458 → 43.917 ms | 137.415 → 95.854 ms | 158.781 → 109.313 ms |
| 256 KiB / 4 | 9.691 → 6.034 ms | 9.917 → 6.171 ms | 36.256 → 31.208 ms | 46.605 → 40.110 ms |

For two 5 MiB streams, complete-command p50 falls **30.2%**, p95 **31.2%**.
Decoder p50 falls **45.1%**, p95 **46.1%**. Decoder peak allocations fall
**43.60 → 21.76 MiB (50.1%)**; complete-command peak allocations fall
**70.63 → 48.79 MiB (30.9%)**. Median maximum complete-command heartbeat gap
falls **95.422 → 54.398 ms**, with p95 **107.014 → 62.082 ms**.

Small-command latency is dominated by variable shell/process scheduling; the
observed multi-millisecond differences cannot be attributed to a decoder change
of tens of microseconds. These are local integration/CPU results, not QEMU,
SSH/network, production SLO or provider-latency measurements. The existing
transport still buffers full output, and large decoding still occupies the loop.

## Verification

- Before implementation: 79 existing runner tests and 11 subtests passed.
  All 29 added characterization cases also passed against the original parser.
  No pre-existing failure was observed in these relevant selections.
- Final full runner suite: **1,014 passed, 8 skipped, 36 subtests passed**.
  Optional environment/release-artifact tests remain skipped. The suite includes
  real loopback SSH transport coverage; the benchmark itself uses local processes.
- Focused backend accessor/tool/capture/adapter-boundary checks: **157 passed**,
  with two existing Django deprecation warnings.
- Added regression coverage includes empty and wrapped streams, binary/Unicode
  output, marker-like command text, Unicode/ASCII whitespace, malformed base64,
  non-ASCII validation, missing/reordered/repeated markers, absent exit markers,
  real large-output commands with zero/nonzero exits, concurrent results,
  rejected workdirs, runtime failure and cancellation propagation.
- An allocation-budget test for two 2 MiB streams rejects the old implementation
  (18.29 MB peak vs an 11.33 MB budget); the new parser uses 9.12 MB. It avoids
  wall-clock assertions. A seeded 256-payload test checks legacy decoding/error
  behavior, and an additional 10,000-frame baseline/current comparison matched
  values or exception types/messages, including unusual marker arrangements.
- Ruff lint/format checks with the backend's explicit Python config and
  `git diff --check` pass. Final review found no adapter, business-logic,
  lifecycle, state-sharing or unrelated production change.

Frontend/browser/device checks do not apply to this runner transport change.
Pending-question indexing, bounded tool-output truncation and large-tree file
autocomplete remain separate follow-up candidates.
