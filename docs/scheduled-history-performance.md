# Scheduled-run history reads

Baseline: `8e818b0` from `origin/dev`. Both AGENTS files were read, the clean
worktree was fetched/reset as requested, and local/remote branches were checked.
The existing performance branches are already ancestors of `dev`. No user
changes needed stashing.

## Investigation and random selection

Backend repositories/services, runner Git/files/metrics execution, Vue markdown
and chat state, native transcript publication, and shared client/chat helpers
were inspected. Ranking balances impact, path frequency, evidence, complexity,
regression risk, and architectural fit:

| Rank | Opportunity | Current evidence | Impact and tradeoff |
| --- | --- | --- | --- |
| 1 | Backend pending-gate lookup indexes | 500 sessions and 60,000 resolved permission/question rows: attention projection takes 32.7 ms p50. SQLite EXPLAIN shows a full permission-table scan and temporary ordering tree; neither request model declares lookup indexes. | Frequent conversation feed/timeline/MCP reads; strong evidence. Local index changes need migration and write-cost verification. |
| 2 | Runner sequential untracked Git diffs | Public working-diff operation with 50 new files executes 55 runtime calls: 250.8 ms p50 locally, 967.7 ms with a simulated 10 ms delay per call. | User-visible Git refresh latency; strong command-count evidence. Batching/concurrency must preserve containment, per-file failures, repository locking and SSH bounds. |
| 3 | Backend scheduled-run history overfetch | 100 runs with 64 KiB answers: service/presenter probe allocates 7.1 MB for a 35 KB response. Both history and completion-repair queries select answer bodies and unused related objects. | History pages and MCP task inspection, plus completion reconciliation. Large allocation reduction with a small repository-only change and low regression risk. |
| 4 | Shared conversation grouping | Pure shared helper: 20/100/500 workspaces with ten chats each take 0.68/3.44/26.11 ms p50. Sort comparisons repeatedly parse dates and construct locale comparison options. | Shared web/mobile navigation; larger organizations may benefit. Actual UI frequency/render cost needs profiling before choosing an algorithm change. |
| 5 | Mobile session metadata publication | Real controller with synthetic REST/realtime inputs: one/100/500 sessions take 0.017/0.113/0.515 ms p50 per delta, excluding native rendering. Metadata is cloned/frozen on each publication. | Frequent path, but low measured controller CPU cost; recent transcript sharing already addresses the larger cost. Preserve immutable snapshots. |
| 6 | Web full-document streamed markdown | Source inspection confirms full parsing/sanitization, already coalesced every 50 ms. | Potential long-answer cost, but browser profiling is needed; reference-link/media behavior makes incremental parsing riskier. |

The suspected serial VM-probe candidate was rejected: the current registry
already uses concurrent collection. Existing stream/history/Git optimizations
were checked to avoid repeating their work.

The top three were fixed before Python `secrets.randbelow(3)` returned **2**,
selecting scheduled-run history. There was no redraw. The recorded draw is in
[selection.json](benchmarks/scheduled-history/selection.json). Exploratory timings
above are separate from the controlled before/after measurements below.

## Change

`ScheduledTaskRepository.list_runs` and `active_runs` still join the assistant
message for its outcome, but defer its unused `content` column. They no longer
hydrate unused session objects; completion repair also omits the unused task
join. Ledger fields, assistant finish/error/completion metadata, filtering,
ordering, and the 100-row default limit retain their existing behavior.

REST and MCP continue calling `ScheduledTaskService.list_runs`; the service's
completion repair and conditional writes are unchanged. Reconciliation and eager
completion also benefit through `active_runs`. No adapter, service, runner,
frontend, protocol, cache, dependency, migration or retained state was added.
Existing callers were inspected: none consumes deferred answer content or the
removed eager relations. Regression tests render both REST and MCP presenters
inside the query budget to detect accidental per-row lazy reads.

## Measurements

Python 3.12.3 and Django on Linux, using file-backed disposable SQLite databases
and the authenticated Django HTTP test client. The real endpoint includes
authentication, ownership checks, service repair, ORM hydration, serialization,
and response validation. Fixtures use distinct sessions across native/Claude/Codex,
mixed success/error outcomes, configuration snapshots, and 0/64 KiB answers.
Repair cases reset one completed error run to `running` before each request;
that reset is outside timing and instrumentation.

Each batch has five warmups and 31 timed requests per case. An initial full
1/20/100-run matrix ran baseline then optimized. The 100-run matrix was repeated
in optimized/baseline order, loading the unchanged baseline backend from a Git
archive. No tests ran alongside timing. The table pools 62 samples per version;
p95 uses nearest rank, and allocation peaks use the larger of the two separate
tracemalloc observations. SQL capture runs separately at the cursor boundary
to include Django worker-thread connections.

| 100-run workload | p50 before → after | p95 before → after | Peak Python allocations before → after |
| --- | ---: | ---: | ---: |
| Empty answers, settled | 36.627 → 31.749 ms | 52.851 → 42.671 ms | 712,495 → 570,576 B |
| Empty answers, repair | 48.926 → 43.377 ms | 55.891 → 50.734 ms | 718,340 → 575,431 B |
| 64 KiB answers, settled | **56.469 → 40.437 ms** | **62.971 → 49.813 ms** | **7,270,216 → 572,502 B** |
| 64 KiB answers, repair | 58.206 → 45.035 ms | 69.116 → 53.109 ms | 7,342,728 → 573,445 B |

The large settled history improves p50 by **28.4%**, p95 by **20.9%**, and peak
Python allocations by **92.1%**. It avoids loading 6.25 MiB of answer text for
the same 39,935-byte response. HTTP query counts remain **7** for settled history
and **8** with repair; answer-body SELECTs fall from **2 to 0**. Every response
SHA-256 matches between versions across both batches.

The 20-run, 64 KiB case reduces allocations from 1,542,260 to 176,896 bytes,
but p50 changes only from 22.929 to 22.537 ms. Single-run latency is noisy and
does not establish a uniform speedup. These are local warm synthetic workloads,
not production SLOs or PostgreSQL/network measurements. Tracemalloc measures
Python allocations, not process RSS or database memory.

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_scheduled_history.py
# Repeat the same script with an unchanged baseline backend:
baseline_dir=$(mktemp -d)
git archive 8e818b0 backend | tar -x -C "$baseline_dir"
backend/.venv/bin/python scripts/benchmark_scheduled_history.py \
  --backend-root "$baseline_dir/backend"
```

Raw timings, counts, allocations and hashes are in
[before.json](benchmarks/scheduled-history/before.json),
[after.json](benchmarks/scheduled-history/after.json),
[before-repeat.json](benchmarks/scheduled-history/before-repeat.json), and
[after-repeat.json](benchmarks/scheduled-history/after-repeat.json).

## Verification

- Before edits: **145 passed, 4 skipped** in scheduled-task and MCP task tests.
- The first five regression cases fail on the original body SELECTs. Final
  coverage adds empty histories and independent message/session deletion cases.
- After edits: **152 passed, 4 skipped**, including seven new regression cases.
  Coverage checks empty/single/over-limit history, ordering, outcomes and snapshots,
  deleted links, unfinished turns, scope, completion repair and deferred auto-stop.
  Existing REST/MCP ownership/permission, recovery and SQLite lifecycle tests pass.
- The four PostgreSQL-only concurrency tests remain skipped on SQLite; no live
  PostgreSQL concurrency or browser/device tests were performed for this query change.
- New tests and benchmark pass Ruff with `backend/pyproject.toml`. The repository
  file has no new Ruff diagnostics: its three pre-existing diagnostics become two
  (an unrelated import order and long line remain). `git diff --check` passes.
- Final review confirms changes are restricted to the two repository reads,
  regression coverage, reproducible benchmark and this report. No user work or
  reference repositories were changed.
