# Batched chat-fork persistence

## Investigation and selection

Baseline: `3ada338f5e5e5820aa362a747d9533f35580a208` on `origin/dev`.
Both AGENTS files were read in full. The worktree was clean; the requested
untracked-inclusive stash found nothing to save. Local and remote branches were
checked, origin fetched, and `dev` reset to `origin/dev` before code inspection.
Previous history, plugin, streaming, Git, directory-download and frontend
performance work was already integrated and excluded.

The review covered backend repositories/services, runner files/Git/streams,
web chat projections, mobile rendering/snapshots, and both shared packages.
Candidates were ranked by impact, frequency, measured evidence, implementation
risk and architectural fit. The first three formed the shortlist:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend chat-fork copying | 10/50 turns with five assistant parts made 354/1,754 service SQL statements. The authenticated 50-turn REST baseline took about two seconds. Repeated position queries, individual inserts, message updates/reloads and durable commits are avoidable. Moderate ordering/transaction risk, isolated to repositories. |
| 2 | Runner archive uploads | Real `FileManager.upload_file`, a 5,000-file, 60,479-byte compressed archive and an async fake VM transport stalled a 1 ms heartbeat for a median 354.8 ms across five samples. Synchronous tar conversion blocks all runner work; cancellation/admission and bounded execution require care. |
| 3 | Shared streaming reducer | Real `applyPartDelta` over Vue reactive state, 40 messages, 500 old parts and 1,000 text deltas took 214.6 ms median across five samples; 50 parts took 39.8 ms. Historical parts are scanned per delta. Frequent across clients, but preserving mutable/immutable consumer ownership makes indexing changes riskier. This is a Node CPU probe, without browser rendering. |
| 4 | Mobile snapshot publication | `ChatController.buildSnapshot` clones/freezes all session rows and derives lineage on every publish. The current code already shares unrelated message identities. Plausible high-frequency work; device profiling and ownership analysis still needed. |
| 5 | Runner mention search | `build_find_files_command` sorts all matching paths before keeping at most 51. Large-tree traversal/sorting remains plausible; realistic filesystem measurements and ordering requirements must guide any change. |
| 6 | Web chat projections | `HarnessMessageView.vue` derives block and agent-step views when streaming parts change. Prior optimizations already bound rendering and coalesce Markdown. Further work needs browser frame/render evidence. |

A single uniform OS-random draw, Python `secrets.choice`, over
`[backend_fork_copy, runner_archive_upload, shared_stream_reducer]` selected
**backend_fork_copy**. No redraw or optimization of another candidate followed.
Exploratory probes could overlap test work; the final REST measurements below
run without competing test suites and supersede the exploratory timings.

## Change and boundaries

`HarnessMessageRepository.copy_prefix` allocates new IDs and consecutive
destination positions once, then inserts complete message rows in batches of
50. It no longer creates, updates and reloads each message separately.
`HarnessPartRepository.copy_for_messages` reads source parts in bounded message
groups and streams/inserts at most 50 part rows per batch. New messages need no
per-part position query. The part repository still owns metadata sanitization
and uses the existing canonical display builder.

One transaction owns all copied messages/parts and the final user-activity
timestamp. A destination-session `FOR NO KEY UPDATE` lock serializes competing
copies on PostgreSQL while allowing ordinary appenders' foreign-key checks.
Position collisions with ordinary appends retry the complete transaction, up to
the existing five-attempt limit. SQLite uses its configured immediate writer
transaction. The copy takes no source-row lock and still works for busy sources.

Cutoff rules, fresh UUIDs/timestamps, gap-tolerant source ordering, append-to-
destination behavior, usage/engine/skill fields, dismissal state, compaction
reference remapping, cleared subtask child links and regenerated displays are
preserved. There are no model save overrides or message/part save receivers to
bypass. REST/Socket.IO/MCP contracts, services, adapters and runner execution
remain unchanged. There is no new dependency, cache, retained state or schema.

## Measurement

`scripts/benchmark_harness_fork.py` creates and migrates a disposable file-backed
SQLite database, then calls the authenticated REST fork endpoint with real
middleware, authorization, services, repositories and response serialization.
Only outgoing Socket.IO transport is replaced. Each source contains 10 or 50
user/assistant turns, five parts per assistant (text, tool, reasoning, subtask,
compaction), and 7–14 KiB of output per part.

Each version gets one warmup and 21 timed requests per workload per batch.
Destination validation/deletion, SQL capture and `tracemalloc` probes are outside
the latency window. Every copy is checked with a normalized SHA-256 digest over
all replay/usage fields, part bodies, displays and remapped references. IDs and
fresh creation timestamps are intentionally excluded. Complete digests agree
between versions and samples. The baseline is an archive of the unchanged
backend, using the same interpreter and benchmark script.

Two sequential batches reversed version order: baseline/optimized, followed by
optimized/baseline. Pooling the 42 samples per version gives nearest-rank p95:
Environment: Linux, Python 3.12.3, Django 5.2.17, SQLite 3.45.1.

| Turns / copied rows | REST SQL statements before → after | p50 before → after | p95 before → after |
| --- | ---: | ---: | ---: |
| 10 / 20 messages + 50 parts | 366 → 24 | 361.267 → 38.936 ms | 444.993 → 49.591 ms |
| 50 / 100 messages + 250 parts | 1,766 → 30 | 1,902.876 → 96.116 ms | 2,413.752 → 113.482 ms |

At 50 turns, median request latency falls **94.9%**, p95 falls **95.3%**, and
SQL statements fall **98.3%**. Explicit copy commits drop from 350 to one.
Batch CPU medians drop from 914–987 ms to 76–77 ms for that workload (Python
process CPU time including worker threads, excluding I/O wait).

The tradeoff is additional bounded temporary allocation: Python peaks are
0.381–0.386 → 1.014–1.017 MB at 10 turns and
0.732–0.737 → 1.690–1.692 MB at 50 turns. Bulk SQL parameter construction and
50-row payload batches raise the peak by less than 1 MB here. Pending part
batches are cleared after insertion; the optimization retains no payload cache.
Message snapshots and the ID map still scale with transcript length, as before.

Results and individual latency samples are retained in
[the benchmark artifact](benchmarks/harness-fork.json). SQL totals include
transaction commands and the authenticated request's fixed overhead. Python
allocation peaks are not process RSS. These are local integration measurements,
without production PostgreSQL/network/provider latency; they are not production
tail-latency guarantees.

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_harness_fork.py --samples 21
backend/.venv/bin/python scripts/benchmark_harness_fork.py \
  --backend-root /path/to/archived/backend --samples 21
```

## Verification

Before changes, 65 existing fork/edit service and REST tests passed. Eight new
field/cutoff/display characterization cases also passed against the unchanged
implementation. Exploratory runner file/security/capture tests passed (73), as
did shared package tests (67).

New regression coverage checks query budgets across batch boundaries, all copied
message fields, consecutive positions, fresh IDs, source immutability, legacy
patch sanitization, compaction/subtask references, display rebuilding, concurrent
copies, a forced copy/ordinary-append position race, rollback after a later part
batch, bounded batch sizes and successful whole-copy retry.

Final relevant checks:

- SQLite: **266 passed, 1 skipped**, covering fork/edit service and REST,
  message/part persistence, timeline projections, harness services/sockets,
  Claude/Codex sessions, capture guards and adapter boundaries. The skip is the
  PostgreSQL-only forced position race. The matching unchanged-baseline suite
  passed all **252** existing cases.
- PostgreSQL 16.15: all **15 new regression cases passed**, including both
  concurrent-copy and forced ordinary-append races, rollback/retry and batch
  budgets. This used a separate local cluster and disposable test database;
  worker connections are explicitly closed before teardown.
- An earlier expanded SQLite run had one transient failure in
  `test_account_revocation_stops_claude_owner_and_releases_attempt` (the finish
  field was still empty). This test does not fork. It passed isolated against
  both versions and in the complete baseline/current reruns above. It was not
  reproducible on demand, so it is recorded rather than claimed as a proven
  pre-existing failure or changed outside this optimization's scope.
- Ruff lint passed on all changed Python files. New files and changed production
  ranges pass formatting; unrelated pre-existing formatting was left intact.
  `git diff --check` passed. Final diff review found no unrelated feature change,
  adapter ORM access, new dependency, retained cache or protocol change.

Existing warnings include Django/Ninja deprecations, missing test staticfiles
and two unawaited test-coroutine warnings. No UI code changed; browser/device
checks and production-traffic benchmarks were not performed.
