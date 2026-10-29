# Batch encrypted transcript persistence

Started from `origin/dev` at `84c0055`. Both AGENTS files were read in full,
local/remote branches were checked, an include-untracked stash was attempted
(the checkout was clean), and `dev` was fetched/reset before inspecting code.
The two existing performance branches were already merged. Reference repositories
and existing user work were left unchanged.

## Investigation and selection

The survey covered Django services/repositories, runner file/command execution,
Vue streaming Markdown, native transcript publication/list rendering, the shared
reducer, conversation grouping and client transport. Candidates were ranked by
expected latency/responsiveness gains, frequency, evidence, implementation risk
and architectural fit:

| Rank | Candidate | Evidence and impact | Risk / fit |
| --- | --- | --- | --- |
| 1 | Backend transcript append | Real ORM scout: 50/200 new entries require 105/405 statements and 52.5/194.7 ms p50. SDK mirror frames are coalesced by transcript path and appended in batches; checkpoint persistence uses the same repository. | Local repository change; preserve transactional serialization, UUID idempotency, encryption, order and unkeyed entries. |
| 2 | Backend pending question reads | Current repository SQL against disposable SQLite with 50,001 retained rows scans the whole table and sorts. One/50 session scopes take 5.84/11.28 ms p50 despite returning one row. | High confidence, frequent timeline/feed path; an appropriate index needs migration and write-cost verification. |
| 3 | Runner file-read parsing | Actual `FileManager.read_file`, with a prebuilt 5 MiB guest response: 17.02 ms p50 and 19.38 MB peak Python allocations, excluding SSH/network. The base64 body is split into every line and rejoined synchronously. | Local parsing optimization; preserve whitespace, header and error semantics. |
| 4 | Runner file-read guest calls | Each read still makes two sequential guest calls: realpath containment and stat/MIME/content. | Potential network latency gain, but containment/error behavior makes combining commands riskier than parsing alone. |
| 5 | Shared descendant traversal | `collectDescendantSessionIds` repeatedly scans all sessions during native snapshot publication. | Source evidence only; bounded agent depth reduces normal-workload value. Shared ownership must remain in chat-core. |
| 6 | Web streaming Markdown | Full-source parse, sanitization and DOM traversal on each rendered update, already coalesced at 50 ms. | Browser profiling needed; incremental parsing risks links, media and sanitization behavior. |
| 7 | Mobile streamed message sorting | The virtualized list sorts the immutable message array on each accepted delta. | Source evidence only; native rendering/interaction measurements are needed before changing ordering or subscriptions. |

A conversation-feed field projection was also measured and rejected: 203 small,
representative session rows took 6.54 ms p50 with the existing read and 7.24 ms
with the experimental projection. It offered no credible measured gain.

The fixed top three were passed to one OS-random `secrets.randbelow(3)` draw.
It returned **0**, selecting transcript append batching. No redraw occurred.
The timestamp, shortlist and scout results are in
[the selection record](benchmarks/transcript-append/selection.json).
Only the selected candidate was changed.

## Implementation and architecture

`HarnessEngineRepository.append_transcript_entries` previously performed one
duplicate lookup and one insert for every new keyed entry, inside the session's
write transaction. A retry repeated every lookup even when all entries existed.

The repository now loads matching digests in bounded batches and inserts new
rows with Django `bulk_create`. A batch-local digest map recognizes duplicates
already staged in the same call, preserving first-write-wins behavior and the
returned input order. Unkeyed entries still receive independent digests and
positions. Lookups are capped at 500 keys and inserts at 100 rows, including
for larger transcript imports. Django also respects each database's parameter
limit. The insert cap bounds PostgreSQL's transient SQL-buffer overhead without
returning to per-entry statements.

The existing session UPDATE/write lock, binding checks, unique constraints,
transaction boundaries and IntegrityError retry loop are retained. A failed
attempt rebuilds its rows after rollback. Ciphertext remains encrypted per entry;
existing entries are not re-encrypted. Returned rows have persisted UUIDs,
timestamps and model state. This model has no custom save hook or save signal
that bulk inserts would bypass.

ORM remains entirely inside the repository; the SDK store and Codex checkpoint
caller continue to use the same implementation. REST/Socket.IO/MCP contracts,
provider behavior and runner/frontend responsibilities are unchanged. There is
no dependency, migration, background work, persistent cache or duplicated state.
Additional maps/lists live only for the append call and scale with its input.

## Measurement

Run from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_transcript_append.py \
  --baseline-ref 84c0055 --samples 21
backend/.venv/bin/python scripts/benchmark_transcript_append.py \
  --baseline-ref 84c0055 --samples 21 \
  --postgres-socket /path/to/disposable/postgresql/socket --postgres-port 55439
# Repeat either command with --sizes 2 10 for the small-batch measurements.
```

The script migrates disposable storage and uses real repository transactions,
canonical JSON, Fernet encryption and ORM writes. Each synthetic assistant entry
contains 2 KiB of text. OpenCuria explicitly configures `session_store_flush="eager"`
in `engines/claude/engine.py`: each frame schedules a background drain. The SDK
coalesces pending frames by transcript path and serializes append calls.
Its default 500-entry/1 MiB buffering thresholds are **not** OpenCuria's configured
flush policy. Sizes 1/2/10 cover small batches; 50/200/500 are larger-batch/import
sensitivity workloads. The production distribution of batch sizes was not
measured. Modes are fresh, replay (all UUIDs already exist), and mixed (half
already exist). Sessions are already bound, as for subsequent flushes.

`DEBUG=False`; three warmups precede 21 samples. The paired run loads the old
repository from Git against the same unchanged models/database and alternates
implementation order. Fixture deletion/seeding, SQL counting, decrypting output
for equality and allocation tracing occur outside timings. Every measured call
checks position order; separately captured outputs must decrypt to exactly the
input entries with identical SHA-256 signatures across implementations.

Timings include commit time and exclude the SDK async bridge, provider, network
and UI. Python allocation peaks are transient allocations, not process RSS.
These are local integration measurements, not production latency/SLO claims.

Raw initial baseline: [before.json](benchmarks/transcript-append/before.json).
Paired SQLite results: [sqlite-paired.json](benchmarks/transcript-append/sqlite-paired.json).
Small batches: [sqlite-small.json](benchmarks/transcript-append/sqlite-small.json).

| Entries | Mode | SQL statements before → after | p50 ms before → after | p95 ms before → after |
| ---: | --- | ---: | ---: | ---: |
| 1 | Fresh | 7 → 7 | 7.181 → 7.024 | 10.909 → 10.439 |
| 2 | Fresh | 9 → 7 | 6.795 → 5.736 | 9.311 → 7.597 |
| 10 | Fresh | 25 → 7 | 22.920 → 10.972 | 27.475 → 13.361 |
| 50 | Fresh | 105 → 7 | 53.560 → 13.751 | 68.151 → 15.002 |
| 200 | Fresh | 405 → 8 | 194.837 → 35.109 | 225.001 → 36.243 |
| 500 | Fresh | 1,005 → 11 | 470.185 → 80.046 | 535.228 → 149.581 |
| 500 | Replay | 505 → 6 | 329.270 → 15.954 | 339.693 → 17.464 |
| 500 | Mixed | 755 → 9 | 405.631 → 48.887 | 432.869 → 115.189 |

Fresh 50/200/500-entry SQLite batches improve p50 by **74.3% / 82.0% / 83.0%**.
The 500-entry replay improves **95.2%**. Single-entry statement counts are
unchanged; the small timing difference is not the purpose of this change.
Larger p95 outliers remain visible in the raw samples and are not discarded.

Batching adds modest transient allocations: fresh 50/200/500-entry SQLite peaks
are 390,054 → 431,365 B, 1,362,388 → 1,458,424 B and 3,294,842 → 3,316,434 B.
This is a measured tradeoff (about 41/96/22 KB), not a memory-saving claim.
There is no state retained between calls, and larger imports still use bounded
SQL batches. Statement reduction shortens the same transaction's lock duration.

Paired PostgreSQL results:
[postgres-paired.json](benchmarks/transcript-append/postgres-paired.json),
[postgres-small.json](benchmarks/transcript-append/postgres-small.json).
The disposable local PostgreSQL 16 instance used normal fsync and Unix sockets.

| Entries | Mode | SQL statements before → after | p50 ms before → after | p95 ms before → after |
| ---: | --- | ---: | ---: | ---: |
| 1 | Fresh | 7 → 7 | 4.924 → 4.423 | 7.055 → 8.735 |
| 2 | Fresh | 9 → 7 | 7.678 → 5.716 | 9.700 → 6.846 |
| 10 | Fresh | 25 → 7 | 17.948 → 6.299 | 19.292 → 6.565 |
| 50 | Fresh | 105 → 7 | 74.762 → 13.993 | 79.964 → 14.557 |
| 200 | Fresh | 405 → 8 | 332.265 → 49.424 | 439.343 → 56.932 |
| 500 | Fresh | 1,005 → 11 | 863.609 → 129.513 | 1,094.655 → 145.682 |
| 500 | Replay | 505 → 6 | 520.441 → 20.854 | 680.365 → 27.500 |
| 500 | Mixed | 755 → 9 | 635.470 → 66.489 | 682.853 → 84.420 |

Ten-entry fresh appends improve p50 by **52.1% on SQLite / 64.9% on PostgreSQL**;
two-entry batches improve 15.6% / 25.6%. PostgreSQL's 500-entry fresh/replay cases
improve **85.0% / 96.0%**. Singleton statement counts are unchanged and singleton
p95 varies, so no singleton latency improvement is claimed. Repeated SQLite
runs and both databases consistently confirm the batch improvement.

The initial 500-row insert cap produced a PostgreSQL allocation peak of
7,660,331 B for 500 fresh entries (baseline 3,366,817 B). That measured increase
motivated the final 100-row insert cap. Final peak: **3,348,842 → 4,031,847 B**,
about 683 KB extra instead of 4.29 MB. Fifty/200-entry fresh peaks are
413,825 → 792,319 B and 1,408,725 → 2,173,431 B. This bounded memory/SQL-buffer
tradeoff is explicit. At 500 entries, lookups/inserts fall from 1,000 statements
to six, plus transaction/session overhead.
The initial PostgreSQL results are retained in
[postgres-paired-initial.json](benchmarks/transcript-append/postgres-paired-initial.json).

## Verification

- Before production edits: 13 persistence/store/activity tests and 29 existing
  engine/API/checkpoint tests passed; 15 pinned-binary tests were skipped.
  Runner investigation checks passed 49 tests. Ten new characterization cases
  also passed against the old repository.
- The new 500-entry query budget failed against the original implementation
  with 1,006 statements for an initially unbound session. It passes after the
  change, including the replay budget.
- Final relevant SQLite backend run: **117 passed, 38 skipped**. It covers
  transcript persistence, the SDK store, Claude engine, Codex checkpoints,
  durable runs, API/engine contracts and adapter/session-access boundaries.
  All 38 skips require an unavailable pinned Codex binary; these tests do not
  execute in this environment. No live provider or agent execution is claimed.
- Final local PostgreSQL correctness run: **34 passed**, including actual
  overlapping transactions, SDK-store isolation and checkpoint safety. The test
  wrapper closes the SDK bridge's thread-local database connection before
  teardown. The disposable PostgreSQL cluster and storage were removed after use.
- Added tests preserve first-write-wins for existing and within-batch UUIDs,
  append-only unkeyed entries, session/subpath isolation, binding rejection,
  validation/encryption rollback, rollback after insertion and retry, model
  metadata, query-free empty batches and 1,100-entry imports. SQL parameters
  are checked against the database limit. Two real connections verify that
  overlapping writes stay contiguous and share only one keyed entry.
- Changed/new Python files pass Ruff; new files pass formatting checks;
  `git diff --check` passes. No frontend or runner implementation changed, so
  UI/device checks are not relevant to this repository-only optimization.
