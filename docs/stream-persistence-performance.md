# Streamed text: one durable write transaction per delta

## Selection and evidence

Baseline: `8609cb6` on `origin/dev`. Both AGENTS files were read in full; the
worktree was clean (`git stash push --include-untracked` had nothing to save).
All local/remote branches were checked, then origin was fetched and `dev` reset
to `origin/dev` before code inspection. Prior Git, archive, history, conversation
feed and frontend performance work was already integrated and was excluded.
Before pushing, the commit was rebased onto the newly arrived `1ef1c04`
deleted-skill fix. The measured event/persistence paths are identical after rebase.

Investigation covered backend, runner, webapp, mobile and both shared packages.
Candidates were ranked by impact, path frequency, evidence, complexity, risk and
architectural fit. The first three formed the shortlist:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend plugin catalog | Real service/repositories: 10/50 org plugins plus seeded globals made 49/209 queries, p50 26.7/111.1 ms. Frequent settings/composer path; straightforward batching, low risk. |
| 2 | Backend fork persistence | 10/50 turns with five parts per assistant made 352/1,752 SQL statements, p50 375/1,750 ms. Large user-visible delay; less frequent, moderate bulk-copy/ordering risk. |
| 3 | Backend text stream persistence | Every text delta read the part display, wrote the part, then separately wrote accumulated message content. 500 deltas made 1,500 data statements, p50 4,425 ms. Very frequent; transactional/cancellation behavior requires care. |
| 4 | Shared streaming reducer | `harnessReducer.ts` searches messages/parts and scans open streams per chunk. Used by both clients; profiling and compatibility work needed before selecting a new indexing strategy. |
| 5 | Runner mention search | `build_find_files_command` globally sorts matching paths before selecting at most 51 results. Large-tree traversal/sorting needs realistic filesystem profiling; ordering must survive any change. |
| 6 | Web message projections | `HarnessMessageView.vue` derives blocks and agent-step views from changing parts during streaming. Possible repeated work; actual rendering/frame evidence needed. |
| 7 | Mobile streaming snapshots | `ChatController` clones all parts of the changed message and clones session snapshots on publish. Historical messages already share identity; further changes require immutable ownership and device verification. |

A single uniform OS-random draw with Python `secrets.choice` over
`[plugin_catalog, fork_copy, stream_persistence]` selected **stream_persistence**.
No redraw or other candidate implementation was performed. Raw selection and
measurement data are in [the benchmark artifact](benchmarks/stream-persistence.json).

## Change

`HarnessMessageRepository.append_content` now accepts an optional `text_part_id`.
For streamed text it updates message content and appends the same delta to that
message's text part in one transaction. Text displays depend on metadata, not
output, so the redundant display SELECT and display rewrite are eliminated.
The service performs one `sync_to_async` call instead of two per text delta.

The repository writes the message before the part, matching the lock order of
turn finalization. It restores the in-memory message if the transaction fails.
Each delta commits before forwarding; no cross-delta buffering, delayed flush,
cache, new dependency, schema change or extra retained state is introduced.
Reasoning still updates its display through its existing persistence path.
Calls without a text part retain their existing single-message write behavior.

ORM and transaction ownership remain in the repository. Run decisions, event
locks, cancellation checks and forwarding stay in the backend service. REST,
Socket.IO and MCP contracts, provider/permission logic, runner and client code
are preserved.

## Measurement

`scripts/benchmark_stream_persistence.py` exercises the real
`HarnessService._on_runner_event`, repositories and frontend emit seam. Each
sample resets a running message/part, streams 100 ordered synthetic chunks and
checks persisted content, displays, part IDs/positions and event order. SHA-256
digests match between versions and every sample. There is no outer transaction.

Each version/database has one warmup and 31 measured samples. Reset, validation,
SQL counting and Python allocation tracing are separate from latency. All runs
are sequential without competing test work. Databases contain synthetic data
only and are deleted on exit. PostgreSQL uses a separate local cluster with
default durability settings and a Unix socket; SQLite uses a temporary file.
Both versions use the same interpreter and benchmark script, with `--repo`
pointing at an archive of the baseline backend.

Environment: Python 3.12.3, Django 5.2.17, PostgreSQL 16.15, psycopg2 2.9.12,
SQLite 3.45.1. These measure unpaced backend persistence/forwarding overhead,
not provider generation time, network delivery or end-to-end answer latency.

Initial/reverse runs use before→after/after→before order respectively. The
initial pilot used 31,538 bytes of existing output; the finalized benchmark and
reverse runs use exactly 32 KiB. Comparisons are within each matched workload:

| Database / round | p50 ms before → after | p95 ms before → after |
| --- | ---: | ---: |
| PostgreSQL, initial | 545.922 → 355.322 | 698.298 → 391.699 |
| PostgreSQL, reverse | 461.631 → 379.689 | 627.465 → 957.171 |
| SQLite, initial | 938.275 → 950.517 | 1,241.786 → 1,027.388 |
| SQLite, reverse | 960.454 → 762.391 | 1,604.186 → 1,381.523 |

Because batch tails varied, PostgreSQL was additionally measured in **four
balanced batch pairs**, each version getting seven samples per pair:
before/after, after/before, before/after, after/before. All four optimized batch
medians were lower. Pooling the 28 latency samples per version gives:

| Metric, 100 text deltas | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| PostgreSQL p50 | 489.158 ms | 317.569 ms | 35.1% |
| PostgreSQL p95 | 1,467.153 ms | 1,164.485 ms | 20.6% |
| Median of batch CPU p50s | 284.767 ms | 173.092 ms | 39.2% |

CPU time is for the Python process and its threads, excluding the PostgreSQL
server and I/O waits. The single reverse PostgreSQL batch has a p95 regression;
these observations establish a repeatable median/CPU improvement locally, not a
production tail-latency guarantee. SQLite did not show a uniform median gain.

Per 100 steady-state text deltas, data SQL changes from **100 SELECTs + 200
UPDATEs to 200 UPDATEs**. Durable write transactions drop from **200 to 100**.
The optimized trace also includes 100 explicit BEGINs and 100 COMMITs, so total
logged SQL statements including transaction commands increase from 300 to 400.
This change targets unnecessary reads, durable commits and thread transitions.

The allocation probe measures peak Python allocations including the collecting
emit seam, not process RSS. Atomic rollback retains the previous message string
for the duration of a delta; it adds transient memory and no persistent cache.
The reverse-round text probe rose from 337,362 to 372,743 bytes on PostgreSQL
(+35,381 bytes) and from 224,048 to 234,348 on SQLite (+10,300 bytes).

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_stream_persistence.py
backend/.venv/bin/python scripts/benchmark_stream_persistence.py --repo /path/to/baseline
# A local disposable PostgreSQL cluster, reachable as user postgres:
backend/.venv/bin/python scripts/benchmark_stream_persistence.py \
  --postgres-socket /path/to/local/socket --postgres-port 55439
# For each version, alternate groups of seven samples to reproduce the paired run:
# add --kinds text --samples 7, alternating with/without --repo as above.
```

## Verification

- Before implementation: 145 persistence/fork/plugin tests and 213
  service/runner-loop/Claude-event tests passed. No pre-existing failures.
- After implementation: 332 relevant SQLite backend tests passed, covering
  persistence, runs, native loop, Claude events/engine, forks/edit REST contracts,
  sockets, access boundaries and abort recovery. Existing deprecation,
  staticfiles and two unawaited test-coroutine warnings remain.
- All seven new regression cases passed separately on SQLite and PostgreSQL:
  per-delta SQL/commit budget and row-lock order, text/reasoning boundaries,
  persistence before forwarding, rollback after either write, cancellation
  during writing and concurrent producer ordering. Test worker connections are
  explicitly closed before database teardown.
- After rebasing, 117 stream, harness-service, skill-selection and deleted-skill
  integration tests passed. That run also emitted a background-coroutine cleanup
  warning from `test_followup_drops_deleted_skill_from_inherited_selection`.
- Ruff checks pass on both modified backend files, tests and benchmark; new
  files pass Ruff formatting. `git diff --check` passes. No UI code changed;
  browser/device checks and production traffic measurements were not performed.
