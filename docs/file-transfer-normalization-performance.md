# Runner file-transfer normalization

Baseline: `798b307ab6cda542c973df6d91d7b8973a600cd5` on `origin/dev`.
Both AGENTS files were read, local/remote branches checked, and
`git stash push --include-untracked`, `git fetch origin` and
`git switch -C dev origin/dev` ran before inspecting project code. The worktree
was clean, so there were no changes to stash. Existing performance branches
were already merged; the reference repositories were left untouched.

## Investigation and random selection

The survey covered backend, runner, webapp, mobile and shared packages.
Candidates were ranked by impact/frequency, measured confidence, implementation
risk and architectural cost:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend patch display projection | A 3.95-MB, 50,000-line diff takes 154.6 ms p50 / 158.0 ms p95 in `build_part_display`; repeated scans produce a four-line preview. A single-pass projection could reduce CPU and temporary allocations, but must preserve hunk/window semantics. |
| 2 | Runner single-file upload preparation | Initial FileManager probes show a 10-MiB upload taking 33 ms p50 and blocking the loop for up to 40 ms. Full chunk-handler measurements subsequently expose a larger cost: repeated regex whitespace normalization. Central transport code permits a localized change with existing size/ownership/ordering coverage. |
| 3 | Backend completion-repair indexing | The real repository query over 100,000 messages in disposable SQLite takes 63.4 ms p50 / 66.5 ms p95. A temporary `(role, completed_at)` index gives 0.97 ms p50; a timestamp-only partial index is not chosen by this query plan. Runs every minute; production PostgreSQL planning and migration impact need separate validation. |
| 4 | Web streamed Markdown | Full parsing, sanitization and DOM parsing recur at the existing 50-ms coalescing interval. Source evidence only; reference links, media and browser responsiveness require profiling before changing this path. |
| 5 | Mobile snapshot publication | Every accepted event rebuilds session/lineage/gate projections. Immutable part sharing is already optimized; native render/device evidence is needed before changing publication semantics. |
| 6 | Shared descendant traversal | Repeated session scans depend on tree order/depth. A reverse-ordered 1,000-session branching fixture measures only 0.232 ms p50 / 0.359 ms p95, so it is lower priority than the measured I/O/CPU candidates. |
| 7 | Backend todo replacement | `TodoRepository.save` inserts each item individually. Query inspection confirms linear writes, but ordinary lists are short and this path is infrequent. |

The first three formed the shortlist. One OS-random draw with
`secrets.randbelow(3)` returned **1**, selecting runner uploads. There was no
redraw. The exact draw is in
[`selection.json`](benchmarks/file-transfer-normalization/selection.json).

Integration profiling narrowed the selected upload candidate: normalization
costs roughly 2.89 ms per clean 256-KiB chunk, versus 0.117 ms using native
whitespace splitting. A maximum upload has 54 such chunks. A worker prototype
for decoding/tar preparation was discarded: a whole native decode still holds
the GIL, and adding scheduling complexity did not address the dominant cost in
the complete transfer. No worker/lock changes from that experiment remain.

## Final change

`runner/src/chunking.py::normalize_base64` now uses
`"".join(payload.split())` instead of a compiled `\s+` substitution. Python's
native string operation preserves the same Unicode whitespace behavior and
retains every non-whitespace character for subsequent strict validation.

The existing shared runner helper still owns this behavior for browser uploads,
harness writes and outgoing read/download transfers. Its callers are unchanged.
There are no new dependencies, caches, workers, locks or protocol changes.
Size checks, padding validation, chunk order, cancellation, capture admission and
workspace ownership remain in their existing layers. No agent/business logic
was added to the executor or a frontend.

## Paired integration measurements

CPython 3.12.3; three warmups and **31 timed samples per version and size**.
Baseline/current order alternates in one process with the same fixtures/runtime.
The benchmark loads the committed baseline normalizer and FileManager without
changing the worktree. It runs actual runner Socket.IO chunk handlers,
WorkspaceService and capture admission, plus real local `realpath`, `mkdir` and
`tar` extraction. Chunks use the production 256-KiB wire limit and separate
event-loop turns. Complete tar SHA-256 digests match, extracted binary bytes and
file modes are checked, and fixtures are removed on exit.

| File size | Full upload p50, before → after | Full upload p95, before → after | Runner CPU p50, before → after |
| --- | --- | --- | --- |
| 1 KiB | 4.345 → 4.325 ms | 5.049 → 5.079 ms | 2.217 → 2.173 ms |
| 1 MiB | 28.621 → 13.812 ms | 32.126 → 15.286 ms | 26.511 → 11.580 ms |
| 10 MiB | 240.735 → 92.163 ms | 249.252 → 102.392 ms | 238.325 → 89.645 ms |

At 10 MiB, full-upload p50 improves **61.7%**, p95 **58.9%**, and runner CPU p50
**62.4%**. A second independent paired run confirms 242.768 → 90.420 ms p50 and
277.616 → 102.356 ms p95. Small transfers show no material latency change.

A separate `tracemalloc` observation measures 50,616,430 → 50,636,965 peak
allocated bytes at 10 MiB (+0.04%); memory use is effectively unchanged. These
are Python allocations, not total RSS. Worst five-ms probe delay at 1 MiB
improves from 7.187 → 1.759 ms p50. At 10 MiB it remains approximately 32 ms:
whole-file decoding/tar preparation is still synchronous. The improvement at
that size is upload latency and CPU consumption, not the longest event-loop
pause. No real-time scheduling or QEMU/SSH/network/browser result is claimed.
CPU includes runner threads and excludes the local tar child. No tests ran
concurrently with these final measurements.

The existing benchmark was corrected to assign `WorkspaceService.files`;
its obsolete `_files_manager` assignment compared the same manager twice.
Baseline/current transfer normalizers are also explicitly switched and restored.
Preliminary measurements from the obsolete injection and the discarded worker
prototype are excluded from the results above.

Raw results: [`paired.json`](benchmarks/file-transfer-normalization/paired.json),
[`repeat.json`](benchmarks/file-transfer-normalization/repeat.json).

Reproduce from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_directory_upload.py \
  --baseline-ref 798b307ab6cda542c973df6d91d7b8973a600cd5 \
  --file-bytes 1024 1048576 10485760 --samples 31 --warmups 3
```

## Verification

- Before source changes: **49 existing upload/chunk tests passed**.
- Focused final suite: **63 passed**, including 14 new normalization cases.
  Coverage includes every Unicode whitespace codepoint, invalid/non-whitespace
  characters (including invisible characters and a surrogate), mixed inputs,
  multi-megabyte binary round trips, wrapped/unwrapped input and chunk thresholds.
- Backend transfer ownership/capture contracts: **97 passed**; two existing
  Django constraint deprecation warnings.
- Final full runner suite: **1,092 passed, 8 skipped, 36 subtests passed**.
  An earlier full run and isolated Git suite hit the unchanged
  `test_parallel_reads_preserve_sorted_text_binary_and_fallback` ordering
  assertion. Its fixture releases the slow read after *any* other read finishes,
  but asserts that specifically `01.txt` finishes first. The final full rerun
  passes. The archived baseline Git suite also passes (5 tests), as do 10 isolated
  baseline repetitions; the transient failure is not consistently reproducible.
  No Git code/tests were changed or skipped to obtain the passing run.
- Changed source, tests and benchmark pass Ruff lint/format checks using
  `backend/pyproject.toml`; `git diff --check` passes.

No UI behavior changed, so browser/device verification is not applicable.
