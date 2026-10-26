# Git diff traversal consolidation

## Investigation and selection

Baseline: `b60d90f9e5a21b22cc37d534c19ad9e94a07c4a0` on `origin/dev`.
Both AGENTS files were read, local/remote branches checked, and the clean
worktree fetched and reset to the latest `origin/dev` before code inspection.
Existing lifecycle, web resource, Git discovery/state-probe, plugin catalogue
and history optimizations were inspected and excluded from the target.

The investigation covered backend repositories/services, runner file/Git
execution, web chat rendering, native transcript publication and shared
reducers/client code. Candidates were ranked by impact, frequency, evidence,
implementation risk and architectural fit:

| Rank | Candidate | Evidence | Scope and risk |
| --- | --- | --- | --- |
| 1 | Backend storage inspection N+1 | Full service plus JSON serialization on disposable SQLite: 1/12/50 image lines with 4/48/200 resources cost 23/144/562 queries and 16.7/102.3/449.0 ms p50. Each resource loads aliases/edges separately; each image line repeats latest/retention reads. | Large scaling gain on storage refresh; batching must preserve dependency and retention facts. |
| 2 | Backend pending-gate scans | 500 sessions and 60,000 resolved permission/question rows: attention projection costs 31.6 ms p50. SQLite EXPLAIN shows both request tables scanned and temporary ordering trees. | Frequent conversation feed; appropriate indexes need migration and write-cost verification. |
| 3 | Runner repeated Git diff traversal | A 100-file mixed working diff executes 11 guest commands, including three diff traversals per side. Initial local probe: 77.0 ms p50. | User waits when opening/refreshing working diffs and commit details; localized executor change with parsing/error risks. |
| 4 | Mobile streaming publication | Accepted deltas clone all parts of the target message, freeze snapshots and rebuild session projections. | Source evidence only; profile native allocations while preserving immutable snapshots. |
| 5 | Shared session-tree traversal | `collectDescendantSessionIds` repeatedly scans all sessions; mobile snapshot publication calls it. | Source evidence only; depth/order semantics and typical shallow trees limit expected benefit. |
| 6 | Web streamed Markdown processing | Growing text passes through `marked.parse` and rendering updates; existing throttling/virtualization already reduce work. | Needs browser profiling and streaming syntax/media checks before changing it. |

The top three were fixed before an actual OS-random draw with Python
`secrets.randbelow(3)`. Result **2** selected runner repeated diff traversal.
No redraw occurred. The other opportunities remain unchanged.

## Implementation

`GitService._git_working_diff` previously read patches, numstat and raw
metadata in three independent Git commands for each changed side. Commit
details repeated the same work. The shared `_git_diff_file_changes` helper
now requests `--raw --numstat --patch -z` together. `parse_diff_z` separates
the output by record boundaries and delegates to the existing path, hunk and
response-limit parsers. The old three-command helpers were removed.

Git changes unmerged working-tree output to combined raw format when patches
are requested, suppressing numstat for conflicted paths. Those reads use one
metadata command and a separate patch command, retaining the prior response.
Root commits use `show --pretty=format:` to preserve configured text conversion
for patches; other commits retain first-parent diffs. Both use the same helper.

This is command construction and parsing inside the existing dumb executor.
Business logic remains in the backend. There are no new dependencies, caches,
parallel tasks, retained state, migrations or frontend/protocol changes.
Credential sourcing, metadata containment, repository locking, timeout and
cancellation propagation, error redaction and response budgets use their
existing implementations.

## Measurements

Reproduce from `runner/`:

```bash
PYTHONPATH=. .venv/bin/python tests/benchmark_git_diff.py \
  --baseline-ref b60d90f --samples 40
```

Python 3.12.3, Git 2.43.0, Linux x86_64. The baseline service is loaded from
the Git revision without changing the checkout. The existing test runtime
maps workspace paths to disposable repositories and runs real local Git
subprocesses. Five warmups precede 40 samples per implementation/case;
baseline/optimized order is shuffled within each pair with a fixed seed.
Each pair uses exactly the same repository and asserts complete response
equality. p95 is nearest rank. No competing test suite ran during measurement.

Times cover the public executor operation, status, Git subprocesses and
response parsing. Auth/containment probes use the test runtime; SSH, QEMU,
backend transport and browser rendering are not measured or simulated.
The initial exploratory timings above are from a different run; the paired
measurements below provide the before/after comparison.

| Workload | Guest commands before → after | p50 ms before → after | p95 ms before → after |
| --- | ---: | ---: | ---: |
| Working diff, 1 file × 50 lines, staged and unstaged | 11 → 7 | 17.217 → 9.001 | 25.542 → 13.039 |
| Working diff, 100 files × 50 lines, staged and unstaged | 11 → 7 | 122.208 → 92.340 | 256.463 → 190.550 |
| Working diff, 10 files × 5,000 lines, staged and unstaged | 11 → 7 | 211.882 → 207.072 | 368.008 → 271.583 |
| Commit details, 100 files × 50 lines | 9 → 7 | 60.380 → 45.237 | 95.944 → 68.910 |

The 100-file working diff improves **24.4% p50 / 25.7% p95**, with **36.4%
fewer guest execs**. Commit details improves **25.1% p50**. Large-file median
improvement is only **2.3%**: patch generation/parsing dominates that workload.
Local scheduling adds timing variability; eliminated-command counts are exact.

Separate `tracemalloc` probes measure parent Python allocations, not guest
RSS. Peak KiB before/after for the four rows are 109.6/103.3,
5225.5/5300.2, 49149.5/50122.6 and 1426.7/1511.8. Combined-output splitting
adds temporary storage for large patches (about 2% in the largest case),
with no retained cache or resource lifetime change. Existing response caps
remain identical. Results are stored in [benchmarks/git-diff.json](benchmarks/git-diff.json).

## Verification

- Before editing: 173 existing runner Git tests and 14 subtests passed;
  61 backend conversation/inventory/activity investigation tests passed.
- After editing: full runner suite **900 passed, 7 skipped, 37 subtests passed**.
- Backend Git service/REST/Socket.IO and adapter-boundary checks:
  **180 passed**. Existing deprecation/staticfiles warnings remain.
- Fifteen new cases compare real-Git output against separate legacy-format
  reads, and cover command budgets at 1/20/101 files, NUL-delimited unusual
  paths, renames, binary/mode/symlink/empty changes, byte/file patch limits,
  root/regular/merge commits and text conversion, conflicts mixed with ordinary
  changes, redacted
  failures, best-effort commit reads, cancellation and lock release.
- New tests and benchmark pass Ruff lint/format checks. The two production
  files retain the same 17 pre-existing broad Ruff findings; no new findings.
- Final diff checked for unrelated edits, duplicate implementations and
  architectural violations. No UI code changed; browser/device and live
  QEMU/SSH measurements were outside this local executor verification.
