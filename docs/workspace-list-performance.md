# Workspace list metadata batching

Started from `origin/dev` at `80bf10481b59ce36d93ae83fb169a95c093f9e3f`
after reading both AGENTS files, checking local/remote branches, attempting an
include-untracked stash (the checkout was clean), fetching and resetting `dev`.
The existing performance branches were already merged. No reference repo changed.

## Investigation and selection

The survey covered backend, runner, webapp, mobile and shared packages. Ranking
weighed measured cost, frequency, confidence, implementation risk and ownership:

| Rank | Opportunity | Evidence and expected impact | Risk / scope |
| --- | --- | --- | --- |
| 1 | Backend workspace-list metadata | Real ORM/activity/schema path: 1/25/100 empty workspaces used 5/53/203 queries and 3.970/28.767/108.147 ms p50. Credentials and plugin IDs were read separately per row; images added further relation/latest queries. Every workspace discovery request pays this cost. | High confidence; read-only batching within existing services/repositories. |
| 2 | Runner exec-output decoding | Current parser, two 10 MiB GNU-base64-wrapped streams: 183.569 ms p50, 87.19 MiB peak additional Python allocations. Repeated frame splits and whitespace removal copy large buffers. Every synchronous harness command uses this parser. | Medium risk: preserve malformed-frame, whitespace and decoding behavior. Parser-only exploratory measurement excludes QEMU/SSH. |
| 3 | Backend tool-output truncation | Current helper, roughly 10 MiB build log: 25.468 ms p50 / 35.44 MiB peak; 10 MiB of short lines: 55.796 ms / 51.99 MiB. It splits and encodes the complete output before retaining a bounded preview. | Medium risk: UTF-8 boundaries, empty lines, clipping direction and omission counts must match. Large outputs are less frequent; helper-only measurement. |
| 4 | Runner file autocomplete | `build_find_files_command` walks and sorts all matching files before returning 51, on an interactive path. | Plausible, not profiled; preserve depth ordering, exclusions and guest portability. |
| 5 | Mobile streaming snapshot metadata | `ChatController.buildSnapshot` clones session/todo/gate snapshots on each publication. Transcript structural sharing is already implemented. | Needs native profiling; lower confidence for normal session counts. |
| 6 | Shared descendant collection | `collectDescendantSessionIds` repeatedly scans all sessions until no additions remain; mobile snapshots call it on publication. | Depth is bounded, so typical impact may be small. No isolated measurement. |
| 7 | Webapp long streaming Markdown | Full parsing/sanitization remains necessary on each rendered update. Existing 50 ms coalescing already limits its frequency. | Low confidence in additional benefit; security and document-wide link semantics constrain changes. |

The shortlist was ranks 1–3, in that order. One actual uniform
`secrets.randbelow(3)` draw returned **0**, selecting workspace-list metadata.
There was no redraw. Only this candidate was changed.

## Implementation

`WorkspaceReadService.describe_many` composes the same dictionaries as detail
reads, using request-local batches of attachment IDs, workspace relations and
image references. The REST list adapter validates these dictionaries with the
existing schema. Detail reads share the same composition implementation.

Credential and plugin services retain ownership of their read ports. Their
repositories return ordered IDs only; provider-secret attachments remain hidden
and inactive plugin selections remain included. Obsolete singleton ID read ports
were replaced rather than retained as unused wrappers.

`WorkspaceReadRepository` preloads relations on the already-authorized objects,
preserving annotations and order. `ImageVersionService` resolves each distinct
image once and reuses `ImageLineRepository.latest_many`: captured lines select
the highest ready, published generation; definition lines use their published
`current_generation`, even when an unselected newer generation exists. Missing,
legacy and unavailable images keep their previous representation.

All ORM access remains in repositories. REST/MCP contracts, ownership checks,
agent execution, runner and clients are unchanged. No dependency, schema,
persistent cache or cross-request state was added. List queries are bounded by
relation/line kinds rather than workspace count (5 bare / 12 populated for the
service plus serialization path, excluding authentication).

## Reproduction and measurement

Run from `backend/`:

```bash
WORKSPACE_READ_BASELINE_REF=80bf104 .venv/bin/pytest -q -s \
  apps/runners/tests/benchmark_workspace_reads.py
```

The opt-in benchmark loads the original read modules from Git and uses their
original repository methods. It compares both implementations against identical
fixtures through the authenticated Django HTTP test client, including permission
checks, real ORM/activity reads, Ninja schema validation and JSON serialization.
The original per-row mapping is adapted only inside the benchmark to traverse
the same HTTP stack. Complete response equality is asserted before measuring.

Each implementation gets 30 warm samples, interleaved in a reproducibly shuffled
order. p95 uses nearest rank. SQL counting and `tracemalloc` peak tracking run
separately from timing; fixture creation is excluded. Every request reads fresh
objects. Populated fixtures attach two visible credentials plus a hidden provider
secret and two inactive plugins to each workspace. Distinct captured/definition
image lines include current/pending versions; every third row is a legacy image.

Environment: Python 3.12.3, Django 5.2.17, SQLite 3.45.1, Linux 6.8. These are
local integration measurements, not production PostgreSQL or network timings.
Allocation peaks describe Python allocations during a request, not process RSS.
The final repeat ran without other test processes. Recorded results and the selection
record are in [benchmarks/workspace-list.json](benchmarks/workspace-list.json).

| Workspaces | Metadata | REST queries before → after | p50 before → after | p95 before → after | Peak MiB before → after |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | Bare | 9 → 9 | 8.223 → 8.463 ms | 10.121 → 10.057 ms | 0.16 → 0.161 |
| 25 | Bare | 57 → 9 | 32.606 → 12.961 ms | 34.383 → 14.319 ms | 0.525 → 0.489 |
| 100 | Bare | 207 → 9 | 111.801 → 28.609 ms | 119.791 → 36.076 ms | 1.724 → 1.634 |
| 1 | Populated | 15 → 13 | 13.502 → 11.888 ms | 14.008 → 12.993 ms | 0.184 → 0.182 |
| 25 | Populated | 159 → 16 | 125.06 → 28.682 ms | 133.797 → 33.583 ms | 0.975 → 0.893 |
| 100 | Populated | 609 → 16 | 473.314 → 70.896 ms | 596.631 → 211.959 ms | 3.445 → 3.219 |

At 100 populated workspaces, query count falls **97.4%**, p50 falls **85.0%**, p95 falls **64.5%**, and peak Python allocations fall **6.6%**. The single bare workspace adds 0.240 ms at p50 (8.223 → 8.463 ms), with unchanged query count and nearly unchanged allocations; populated single-workspace reads improve. No cross-request cache is used to obtain these results.

## Verification

Before implementation, relevant suites passed (101 backend tests, an expanded
187-test backend suite, and 43 runner tests plus 12 subtests). No pre-existing
test failure was found. After implementation, the wider backend selection passed
440 tests; two existing PostgreSQL-only locking/migration tests were skipped on
SQLite. A final 54-test run passed after the last source changes. The opt-in
before/after benchmark passed all six workload combinations.

Regression coverage includes constant query counts at 3/30 populated workspaces,
the complete REST adapter, list/detail response parity, empty results, owner/org
and API-permission boundaries, provider-secret exclusion, ordering, annotation
preservation, failed reads, legacy images, inactive/unavailable image lines and
published definition selection. Reusing a reader after attachment changes and a
new image publication verifies that no request-local facts become stale caches.
Ruff passes for the edited service/repository/test modules. The API module retains
one pre-existing E501 at `api.py:1180`, confirmed against `80bf104`; the changed
adapter line passes. `git diff --check` is clean.

The change is synchronous read batching with no new workers, locks, shared
mutable state or lifecycle behavior. PostgreSQL-specific behavior and production
network latency were not measured. Frontend/device checks are not applicable to
this backend-only change.
