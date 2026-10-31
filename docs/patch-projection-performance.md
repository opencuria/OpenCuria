# Single-pass backend patch previews

Patch persistence now scans a unified diff once, without copying the whole input
into `StringIO` or constructing preview objects for the unused changed suffix.
For a 2.68 MB replacement patch, repository creation and compact-response
serialization improved from **170.753 to 69.948 ms p50 (59.0%)**. Peak traced Python
allocations fell from **10,726,099 to 22,034 bytes**. The response is identical.

## Investigation and selection

Baseline: `55739586fdc3c19672067af571fcdfe69eadbcff`, fetched from `origin/dev`
on 2026-10-31. Both AGENTS files were read in full. The initial working tree was
clean, so no stash was needed. Local/remote branches were checked before code
inspection; existing performance branches were already incorporated in `dev`.
`git fetch origin && git switch -C dev origin/dev` ran before inspection.

The survey covered backend repositories/services and event projection, runner
file/Git/metrics execution, web Markdown/block rendering, mobile snapshots/list
rendering, and shared reducers/client file transport. Existing batched workspace
reads, compact timelines, mobile part sharing, concurrent metrics and bounded
mention ranking were excluded from the target.

Candidates were ranked by impact, path frequency, confidence, complexity,
regression risk and architectural fit. The top three were fixed before the draw:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend duplicate media reads | A complete 1 MiB PDF tool call performs two accessor reads and transfers 2 MiB. Avoiding the duplicate removes a runner round trip; truncated reads, image preparation and ingestion caps need coverage. No network latency was inferred from the in-memory accessor. |
| 2 | Backend repeated patch projection | A 2.68 MB, 100,000-change-line display took 241.216 ms p50 / 256.723 ms p95 and 10.7 MB peak traced allocations. Every patch creation/update and fork uses the projector. A pure, local algorithm change has limited regression scope, provided preview windows/counts match exactly. |
| 3 | Backend edit-history discovery | The two existing history reads for an edit load 52.43 MB of content across 200 messages: 56.317 ms p50 / 62.438 ms p95, 55.46 MB peak traced allocations, two queries. Scoped target/suffix projections could help, but admission and rejection semantics raise risk. |

The timing scouts used 21 warm samples after three warmups on disposable SQLite
or the production pure projector, with tracing separate from timing. The actual
`secrets.choice(top_three)` draw used OS randomness and selected rank 2,
**patch projection**, with no redraw. The selection, scout metrics and other
survey findings are in [selection.json](benchmarks/patch-projection/selection.json)
and [scout.json](benchmarks/patch-projection/scout.json).

Other plausible opportunities include the web's full-document streamed Markdown
parse/sanitize/DOM pass, mobile session cloning/ancestry derivation at publication,
shared reducer part/stream scans, and the runner's separate realpath/find directory
commands. These remain investigation leads, with source evidence only this run.
The shared reducer also has earlier measurements in the repository. No other
production path was changed.

## Implementation and boundaries

[`backend/apps/harness/timeline.py`](../backend/apps/harness/timeline.py) retains
one implementation of patch display construction. The scanner keeps the first
four parsed lines for short/context-only diffs, the preceding context line, and
at most four candidates around the first change. It counts the remaining lines
without creating their display dictionaries. Each retained content field is
capped at the existing 500 characters. One physical line is sliced at a time,
avoiding `StringIO`'s whole-input copy. A very long individual line can still
require temporary storage proportional to that line's length.

Short diffs, single-change trailing context, multiple changes, multiple hunks,
line numbers, empty context lines, Unicode, CRLF, unframed patches, malformed
headers, no terminal newline and change counts retain their existing behavior.
The discarded `in_hunk` flag only reinitialized counters already initialized to
one; hunk headers still reset both counters.

Repositories continue to own all ORM access and invoke this pure projector for
creation, state updates, input updates and fork copies. Run-event orchestration
still awaits persistence before emitting the same bounded display. Full diffs
remain available in stored detail. REST, Socket.IO and MCP contracts are unchanged.
There are no new dependencies, tasks, caches, mutable shared state, migrations or
runner/frontend decisions. All scratch state belongs to one function call;
locking, cancellation and event sequencing are untouched.

## Comparable measurements

Reproduce from `backend/`:

```bash
.venv/bin/pytest -q -s apps/harness/tests/benchmark_patch_projection.py
```

Python 3.12.3, Linux x86_64, SQLite. The opt-in benchmark loads the baseline pure
module from Git without changing the checkout. Both variants run in the same
process against identical disposable fixtures, with three warmups and **31
samples per variant/workload**, shuffling variant order within each pair.
p95 uses nearest rank. No other test suite ran during these measurements.

The integration measurement covers `HarnessPartRepository.create`, including
position allocation and database writes, followed by `timeline_part_payload`
and JSON serialization. Every timed invocation checks exact display/response equality
(excluding the newly generated part UUID), then reads the stored full diff back
and verifies it. Setup, cleanup, readback validation, query instrumentation and
tracemalloc runs are outside the timed region. The temporary part is removed
between calls, so table growth does not affect either variant.

| Workload | Persistence p50 ms, before → after | Persistence p95 ms, before → after | Peak traced Python bytes, before → after |
| --- | --- | --- | --- |
| 129 B, one replacement + 3 context lines | 0.780 → 0.762 | 0.885 → 0.907 | 22,520 → 21,960 |
| 49.9 KB, 1,000 deletions + 1,000 additions | 4.069 → 2.053 | 4.145 → 2.120 | 214,087 → 22,132 |
| 2.68 MB, 50,000 deletions + 50,000 additions | 170.753 → 69.948 | 194.311 → 73.589 | 10,726,099 → 22,034 |
| Stress: one replacement after 100,000 context lines | 181.203 → 87.658 | 192.067 → 92.584 | 3,614,643 → 22,462 |

All workloads execute **four SQL statements in both variants**, including the
pytest transaction's savepoint/release. The tiny case stays around 0.8 ms; its
22 μs p95 variation does not indicate a material latency change. The 49.9 KB case
improves 49.5% at p50. The largest replacement improves **59.0% p50 / 62.1% p95**
and eliminates **99.8% of peak traced Python allocation** on this measured path.

The pure projector is also timed separately: the 2.68 MB replacement improves
165.474 → 64.789 ms p50, with peak traced allocations 10,715,729 → 2,508 bytes.
The large scout started with a change at the first parsed line and triggered
three baseline passes; paired persistence fixtures include leading context and
trigger two. Compare variants within each experiment, not those absolute times
across experiments.

These are backend CPU/local persistence measurements, not production request
SLOs or browser/device frame rates. They exclude provider, runner, SSH, Socket.IO
network delivery and client rendering. tracemalloc measures Python allocations,
not native SQLite buffers or whole-process RSS. PostgreSQL was not benchmarked.
Raw paired samples and memory/query observations are retained in
[`docs/benchmarks/patch-projection/`](benchmarks/patch-projection/).

## Verification

- Before production changes: 103 relevant existing tests passed, with no failures.
- New characterization tests passed on the original implementation; the new
  one-scan regression failed because the same hunk was parsed three times.
- The final opt-in benchmark passed all four integration workloads and exact
  baseline equality across **10,841 generated diffs**: all 9,841 line-kind
  sequences of length 0–8 plus 1,000 deterministic mixed-hunk/Unicode/CRLF cases.
- New integration regressions check full-detail persistence before live emission,
  bounded live/saved projections, state updates, fork copies and private metadata
  scrubbing. Existing authorization, cancellation and concurrency tests are
  included in the final targeted harness suite.
- Final targeted harness suite: **452 passed, 1 skipped** across projection,
  persistence, run timeline, forks/edits, REST/Socket.IO, permission boundaries,
  standard tools, runner loop, harness service and Codex/Claude event projection.
  The skip requires PostgreSQL row locks; this run used SQLite.
- Ruff checks/formatting, Django system checks and `git diff --check` passed.
- Existing Django/Ninja deprecation and missing-static-directory warnings remain.
  API tests also emitted background-task/context cleanup warnings. Replacing the
  projector with the original Git version and rerunning that unchanged API suite
  reproduced the warnings: **72 passed**, confirming they predate this change.
- Frontend and runner code were unchanged; no UI/device check was required.
