# Session summary material reads

Baseline: `535185f`, fetched from `origin/dev` on 2026-10-30. Both AGENTS files
were read, all branches checked, and the requested untracked-inclusive stash
found a clean worktree before switching to the latest `origin/dev`. Existing
performance branches were already integrated.

## Investigation and random selection

Reviewed backend repositories/services, runner Git/files/resource sampling,
web message/block rendering, mobile snapshots/virtualized messages, and shared
chat reducers, conversation grouping and session traversal. These opportunities
remained after excluding already completed work:

| Rank | Candidate | Evidence, frequency and tradeoff |
| --- | --- | --- |
| 1 | Backend summary material | A complete `SessionMetadataService.summary_material()` call over 50 turns took p50 26.230 ms / p95 35.798 ms, three queries and 15,361.7 KiB peak Python allocation. It reads aggregate assistant content and provider/usage/error fields that the summary discards. Runs after each settled turn when a small model is configured. Localized read projection with low execution risk. |
| 2 | Backend fork prefix reads | The already batched copy still loads every source message before discarding the suffix. Copying the first turn of the same 50-turn history took p50 10.543 ms / p95 20.685 ms and 8,200.2 KiB peak allocation. High potential for large early forks, but less frequent and touches cutoff/concurrent-copy semantics. |
| 3 | Backend conversation attention | `attention_for_sessions()` materializes full pending question and permission objects solely to collect their session IDs. With 100 pending questions containing 10 KiB each: p50 5.470 ms / p95 5.805 ms, three queries and 2,262.2 KiB peak allocation. Repeated for conversation feeds; straightforward projection, but smaller absolute benefit. This is a populated-feed stress workload. |
| 4 | Runner Git snapshot reads | One batched metadata RPC is followed by three sequential merge-state RPCs (gitdir, containment, markers). Remote runner latency could dominate. Existing checks intentionally fail closed; safe batching needs careful security and framing validation. No new network benchmark collected. |
| 5 | Mobile snapshot publication | `ChatController.buildSnapshot()` clones/freezes all session/todo metadata and derives lineage on every publication, including streamed tokens. Existing message identity sharing and list virtualization already reduce rendering. Device CPU/allocation measurements are still needed. |
| 6 | Shared descendant traversal | `collectDescendantSessionIds()` repeatedly scans the session list until no descendants are added. Reverse-ordered deep trees increase work; it feeds web/mobile gate and lineage views. Typical depth/size and the current observable ordering need measurement before changing its algorithm. |
| 7 | Web streaming block derivation | `HarnessMessageView.vue` rebuilds message blocks and visible entries when growing parts change. Prior rendering improvements are present; browser frame profiles are needed to establish the remaining cost. |

The first three had the best measured impact/risk ratio. One uniform OS-random
draw, `secrets.randbelow(3)`, returned index **0**, selecting **summary material**.
There was no redraw or implementation of another candidate. Exploratory timings
used 3 warmups and 30 service/repository calls on disposable file-backed SQLite;
setup, cleanup, query capture and allocation instrumentation were outside timing.

## Change and architecture

`HarnessMessageRepository.list_for_summary()` now accepts the assistant ID and
resolves its session-scoped, assistant-role position with a scalar subquery.
Missing, foreign and user anchors return an empty prefix. This removes the
separate full-message anchor read.

The prefix query selects message identity/role and a conditional content
projection. User content is retained in full. Assistant aggregate content is
transferred only for legacy messages without **any** parts; reasoning/tool-only
messages must not fall back to aggregate content either. Unused engine, usage,
error and other message metadata stay deferred. The existing ordered part
projection continues to omit tool/reasoning bodies, inputs and metadata while
retaining text/steer output and the fields needed to select closing prose.

The service consumes this projection and retains all final-answer, steer,
hidden-tool, ordering and no-request rules. ORM stays in the repository; summary
interpretation stays in the service. Public REST/Socket.IO/MCP contracts and
runner/client code are untouched. No cache, background task, dependency, schema
change or persistent state is introduced. Reads retain the existing message /
part query separation. Normal runtime part deletion cascades with its message;
summary writes still recheck the latest assistant and idle status after provider
generation, and account revocation checks remain unchanged.

## Comparable measurements

The opt-in benchmark loads the original repository and service directly from
Git and injects them together. Both versions read identical disposable data in
the same process. Every complete returned material string must match the full
expected result. Three warmups precede 40 interleaved, seeded-shuffled samples
per version/workload; p95 uses nearest rank. Query capture and one-call
`tracemalloc` peak measurement are separate from latency measurement.

Each turn contains a user request and assistant response, 4 KiB of engine
metadata on each message, and (except legacy) progress text, tool output and a
short closing reply. Progress text and tool output have the listed body size;
aggregate assistant content mirrors the progress text. This represents a
long agent conversation with substantial intermediate prose. It is a synthetic
integration workload, not a production traffic percentile. Legacy messages
have no parts and retain their full content fallback.

Linux, Python 3.12.3, Django 5.2.17. PostgreSQL 16 uses a separate local disposable
cluster over a Unix socket; SQLite uses pytest's file-backed test database.

| Database / turns / body size | Queries before → after | p50 ms before → after | p95 ms before → after | Peak Python KiB before → after |
| --- | ---: | ---: | ---: | ---: |
| PostgreSQL / 1 / 4 KiB | 3 → 2 | 2.986 → 3.191 | 3.670 → 4.182 | 75.7 → 81.2 |
| PostgreSQL / 10 / 4 KiB | 3 → 2 | 4.735 → 4.395 | 5.402 → 5.453 | 283.9 → 162.8 |
| PostgreSQL / 50 / 128 KiB | 3 → 2 | 24.353 → 14.072 | 33.940 → 17.896 | 15,309.4 → 7,502.7 |
| PostgreSQL / 10 / legacy | 3 → 2 | 3.610 → 3.379 | 3.772 → 3.564 | 228.6 → 92.7 |
| SQLite / 1 / 4 KiB | 3 → 2 | 2.491 → 2.877 | 3.403 → 3.925 | 75.6 → 79.9 |
| SQLite / 10 / 4 KiB | 3 → 2 | 3.881 → 3.823 | 5.605 → 4.710 | 287.0 → 165.2 |
| SQLite / 50 / 128 KiB | 3 → 2 | 26.171 → 21.003 | 34.320 → 24.550 | 15,325.4 → 7,520.1 |
| SQLite / 10 / legacy | 3 → 2 | 2.766 → 2.778 | 5.722 → 4.943 | 233.8 → 89.9 |

At 50 turns PostgreSQL median latency improves **42.2%**, p95 **47.3%** and peak
Python allocation **51.0%**. SQLite improves **19.7%**, **28.5%** and **50.9%**,
respectively. Query count falls **33.3%**. The one-turn tradeoff is 0.2–0.4 ms
more median latency and 4–6 KiB more temporary allocation for conditional query
construction. This background metadata path benefits most as history grows.
Intermediate text parts still scale with history length; this change removes
duplicate/unneeded reads without changing which material reaches the provider.

These figures exclude provider time, HTTP/network latency and production
contention. Python allocation peaks are not RSS or database memory. No UI code
changed, so browser/device responsiveness measurements were not performed.

Reproduce SQLite from `backend/`:

```bash
SUMMARY_BASELINE_REF=535185f .venv/bin/pytest -q -s \
  apps/harness/tests/benchmark_summary_material.py
```

For PostgreSQL, configure Django's `DATABASES` with a disposable local PostgreSQL
server before invoking pytest. The comparison uses pytest's test database and
does not read application data.

## Verification

- Before implementation: **106 passed, 1 skipped** across summary, fork-copy,
  conversation service and conversation API tests; no failures. The skip was
  a PostgreSQL-only fork position-race test on SQLite.
- New coverage asserts two-query reads at 1/10/50 turns, deferred large message
  and part fields, full expected material, immediate legacy/part changes, and
  one-query rejection of missing/foreign/user anchors. Existing tests cover
  full requests, steers, hidden tools, legacy/error/aborted responses, configured
  model requirements, stale generation and account revocation.
- Expanded SQLite checks: **202 passed** across summaries, conversation service
  and REST, fork/edit REST, timeline projections, capture guards and adapter /
  session-access boundaries.
- PostgreSQL correctness and boundary checks: **123 passed**. Teardown warned
  about one worker connection still using its disposable test database. The
  test process exited and the separate local cluster was stopped and removed.
- Both database comparisons: **4 passed** each, with identical full material on
  every sample. Ruff lint, formatting and `git diff --check` passed.

Warnings include existing Django/Ninja deprecations, absent test staticfiles and
unawaited fake blocking coroutines in fork/edit API tests. No failures were
observed in the expanded SQLite checks. Only the selected read path was changed.
