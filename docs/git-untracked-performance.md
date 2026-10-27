# Bounded parallel untracked Git diffs

## Investigation and selection

Baseline: `d119b79` on `origin/dev`. Both AGENTS files were read in full,
local/remote branches inspected, and the clean worktree fetched and reset to
`origin/dev` before inspecting source. Both existing performance branches were
already merged. Existing tracked-diff consolidation and Git state/discovery
optimizations were inspected; this change targets the remaining untracked-file
path.

The investigation covered backend reads and harness tools, runner files/Git and
metrics, Vue chat rendering, native transcript rendering/publication, and shared
reducers and transport. Ranking considered impact, frequency, measured evidence,
implementation risk, and architectural fit:

| Rank | Candidate | Evidence and expected benefit | Risk / decision |
| --- | --- | --- | --- |
| 1 | Backend workspace-list metadata N+1 | Authenticated REST with 1/10/50 workspaces sharing base/pending captured versions: 14/77/357 SQL queries, 16.399/77.234/316.590 ms p50, 11 samples. Credential/plugin projections and image-version lookups repeat per row. Frequent sidebar/dashboard path. | Medium: batch service-owned projections while preserving image selection and credential visibility. Shortlisted. |
| 2 | Runner sequential untracked diffs | Public `working_diff` with real local Git: 1/20 untracked files produce 6/25 runtime calls and 7.539/40.528 ms in an initial single-sample probe. Every file waits for the previous command; transport overhead accumulates when opening or refreshing diffs after generating files. | Low/medium: independent reads can overlap under the existing repository lock, with bounded tasks/channels and explicit failure/cancellation coverage. Shortlisted. |
| 3 | Backend duplicate media read | Instrumented `ReadTool.execute` on a complete 1 MiB PNG: two accessor RPCs transfer 2 MiB for one attachment. Complete small-media reads could be reused. | Low: retain rereads for truncated media, ingestion limits and error handling. Shortlisted. |
| 4 | Shared/native streaming part traversal | Existing mobile-controller benchmark, 11 samples of 100 deltas, 50 historical messages: 100/500/2,000 parts cost 0.031/0.060/0.183 ms p50 and 0.066/0.121/0.266 ms p95 per event. Reducer still scans parts, but untouched part identities are already shared. | Lower demonstrated impact; preserve immutable native snapshots. Deferred. |
| 5 | Runner file-mention search | `build_find_files_command` traverses matching files and sorts all results before `head`, including for every changed search query. Source evidence only; large-tree profiling needed. | Medium: shallow-first ordering and exclusion semantics must survive any traversal change. Deferred. |
| 6 | Web/native streamed Markdown | Both clients reparse growing text; both already coalesce renders at 50 ms. Mobile uses a virtualized list and memoized message rows. Source evidence only for remaining rendering cost. | Requires browser/device profiling; weaker evidence than the shortlist. Deferred. |

A single uniform `secrets.randbelow(3)` draw selected index **1**, the runner
untracked-diff candidate. No redraw was needed. The initial probes above are
investigation evidence; the paired benchmark below is the final comparison.

## Change and ownership

`runner/src/services/git_service.py` reads untracked diffs in batches of at most
four tasks. One service-owned semaphore bounds active untracked Git commands
across repositories and workspaces, leaving SSH channel capacity for other
operations without a per-workspace cache or extra retained maps.

All commands still use the existing path validator, credential wrapper, command
timeout, parser and fallback. The repository lock covers every batch. Results
are sorted as before. A failed batch drains its children, then raises the first
error in path order and starts no later batch. Caller cancellation propagates
through `gather` to every child, including semaphore waiters. No task survives
the operation's cleanup. The runtime's existing SSH cancellation semantics are
unchanged: cancelling a wait does not kill an already-running guest command.

This remains executor scheduling inside the runner. No business, agent,
permission or provider logic moves between layers. There are no new dependencies,
caches, protocol changes, backend changes or frontend changes.

## Measurement

Reproduce from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_git_untracked.py \
  --baseline-ref d119b79 --output /tmp/git-untracked.json
```

The script loads the baseline Git service from Git without changing the checkout.
Baseline and current implementations run on the same disposable repository in
seeded, shuffled pairs: three warmups and 21 measured samples each. Every response
must have the same SHA-256 across both implementations and all samples. Timing
includes the public operation, status, real Git subprocesses, parsing, and JSON
serialization. Each fixture includes a staged file, a tracked unstaged file,
and 1/20/100 untracked files (roughly 4 KiB each, 10% binary).

The existing test runtime maps guest paths and emulates containment/auth probes;
Git subprocesses are real. Optional 5/20 ms delays on every runtime call model
transport overhead and are explicitly simulated, not observed SSH/QEMU latency.
No test suite runs during measurement. Allocation tracing is a separate probe
of parent Python memory, excluding guest processes and native allocations.

Python 3.12.3, Git 2.43.0, Linux x86_64. Complete paired results and the
recorded random draw are in [benchmarks/git-untracked.json](benchmarks/git-untracked.json).

| Untracked files | Added delay per call | p50 ms before → after | p95 ms before → after | p50 reduction |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 0 ms | 22.288 → 19.846 | 31.272 → 32.850 | 11.0% |
| 1 | 5 ms | 80.989 → 58.601 | 90.649 → 91.183 | 27.6% |
| 1 | 20 ms | 209.252 → 210.556 | 214.309 → 223.160 | -0.6% |
| 20 | 0 ms | 54.433 → 36.985 | 64.130 → 41.691 | 32.1% |
| 20 | 5 ms | 360.378 → 200.757 | 391.089 → 219.184 | 44.3% |
| 20 | 20 ms | 771.545 → 387.692 | 801.755 → 411.348 | 49.8% |
| 100 | 0 ms | 224.957 → 151.392 | 289.973 → 196.512 | 32.7% |
| 100 | 5 ms | 1405.632 → 538.362 | 1544.645 → 674.849 | 61.7% |
| 100 | 20 ms | 2968.834 → 1175.244 | 3238.570 → 1285.444 | 60.4% |

With 100 untracked files and no artificial delay, p50 improves **32.7%** and
p95 **32.2%**. With simulated 20 ms per call, both improve about **60%**.
The one-file case has no parallel work: at 20 ms its p50 increases 0.6% and
p95 4.1%; other one-file timings vary with local scheduling. Multi-file gains
are consistent across the measured delay settings.

Runtime calls remain **8/27/107** for 1/20/100 untracked files. Concurrent
untracked calls change from **1** to **1/4/4**. All response digests match.
At 100 files and zero delay, traced Python peak allocation changes from
**8,130,688 to 8,148,961 bytes** (+17.8 KiB, 0.23%); across all cases the
added peak is below 20 KiB. There is no retained result cache. More Git
processes may briefly run together, bounded at four; guest RSS/CPU were not
measured. Actual deployed-network speedups remain environment-dependent.

## Verification

- Before editing: existing Git suite, 123 tests and 14 subtests passed.
- After editing: full runner suite, 912 tests and 37 subtests passed, 7 skipped.
- Five new tests exercise actual public operations with real Git and controlled
  execution barriers: overlap/task bounds, sorted text/binary/fallback results,
  deterministic first-error propagation and draining, cancellation, timeouts,
  and a shared concurrency budget across repositories, including cancellation
  while waiting for slots.
- Existing security, credential handling, repository serialization and runner
  transport tests remain green.
- Ruff passes for the new tests and benchmark. The edited service retains two
  pre-existing findings (`SIM102` and `S110`), confirmed against baseline source.
- No frontend behavior changed; browser/device checks do not apply.
