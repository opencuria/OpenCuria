# Conversation attention: project pending session IDs

Baseline: `9b9c827c` on `origin/dev`, 2026-11-01. Both AGENTS.md files were
read, branches checked, and the clean checkout fetched/reset before inspection.
Existing performance branches were already contained in dev.

## Investigation and selection

Reviewed backend repositories/services, runner Git/files/resource sampling,
webapp chat virtualization/Git rendering, mobile snapshots/Git drawing, and
shared chat/client helpers. Ranking considered latency, path frequency,
confidence, implementation risk, and architectural fit.

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Runner commit-details parent read | Public operation over real disposable Git data makes 7 runtime calls, including separate metadata, parent and diff reads. Metadata already includes `%P`. Removing the parent read could save a transport round trip, but shallow/replacement behavior needs characterization. |
| 2 | Shared Git SVG simplification | `branchPath` repeatedly splices the remaining segment array. Node/esbuild scout, 31 warmed samples on linear layouts: 10,000 commits p50/p95 11.87/12.98 ms; 20,000 commits 50.78/54.00 ms. Expansion reruns this work in webapp. Shared pure geometry allows a localized linear pass, but benefits mainly large loaded histories. |
| 3 | Backend conversation attention payloads | The service loads complete gate models only to retain `session_id`. With 50 workspaces/four gated children each, that materializes 400 models and parses 200 question bodies on each refresh. A repository projection can remove this work without changing flag semantics. |
| 4 | Mobile snapshot/session-tree work | `ChatController.buildSnapshot` clones all sessions and reconstructs ancestry/descendant scope. Shared descendant discovery repeatedly scans the session array. Needs realistic device/session-depth profiling before changing immutable snapshot behavior. |
| 5 | Runner file-mention search | No-match searches traverse the non-pruned workspace tree. Existing pruning and result caps already help; guest filesystem evidence is needed before changing search behavior or adding state. |
| 6 | Backend feed session projection | Root reads include resumable engine JSON not used by the feed. Current engine metadata appears small; weaker evidence than the pending question bodies, so not shortlisted. |

One actual `secrets.choice` draw over ranks 1–3 selected **rank 3**. No redraw.
Only that bottleneck is changed. Scout Git timings are not production UI timings.

## Change and architectural review

`ConversationService.attention_for_sessions` now asks each gate repository for
distinct pending session IDs. Each query selects only `session_id`, clears
irrelevant chronological ordering, and returns a set. Empty scopes skip SQL.
Full gate reads remain in use by the interaction service, which needs their bodies.

The same service supplies root/child attention flags to consumers. ORM stays in
repositories. Organization/workspace authorization, descendant attribution,
pending-status predicates, response fields and resolution handling are unchanged.
Membership checks already used sets, so removing gate order and duplicate IDs
does not affect results. There is no cache, shared state, migration, new async work,
dependency, runner decision, frontend change, or change to write/cancellation paths.

## Measurement

```bash
backend/.venv/bin/python scripts/benchmark_harness_conversations.py \
  --workspaces 1 10 50 --samples 51 --pending-gates --baseline-ref 9b9c827c
backend/.venv/bin/python scripts/benchmark_harness_conversations.py \
  --workspaces 1 10 50 --samples 51 --baseline-ref 9b9c827c
```

The existing benchmark now supports pending gates and baseline comparison.
It migrates a disposable file-backed SQLite database, uses `DEBUG=False`, and
times authenticated Django HTTP client requests including ownership checks and
JSON serialization. One root/four children per workspace, mixed native/Claude/Codex
roots. With gates enabled, every child has a permission and a question containing
about 3.5 KiB of JSON. This is a concurrent-gate scaling workload, not a claim
about the average number of pending gates in production.

Baseline service source is loaded from Git; its full-record repository methods
are unchanged. Versions alternate order on identical fixtures for 51 warmed samples.
Final runs had no concurrent test workloads. Query counts and `tracemalloc` peaks
are collected separately. Raw results: [conversation-attention.json](benchmarks/conversation-attention.json).

| Workspaces | Gates (permission + question) | p50 ms before → after | p95 ms before → after | Peak Python bytes before → after |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 4 + 4 | 13.136 → 12.625 | 14.392 → 15.281 | 165,902 → 146,854 |
| 10 | 40 + 40 | 18.382 → 16.552 | 22.700 → 20.785 | 623,019 → 218,193 |
| 50 | 200 + 200 | 42.799 → 33.539 | 58.710 → 40.174 | 2,744,689 → 682,990 |

At 50 workspaces, p50 improves **21.6%**, p95 **31.6%**, and peak Python allocations
**75.1%**. All response hashes match; SQL count stays **12** per REST request.
This removes returned data, decoding, model construction and sorting, rather
than round trips. At one workspace, latency changes are noise-level.

Without pending gates, p50 before/after was 14.390/13.808, 20.950/20.153 and
33.814/32.541 ms at 1/10/50 workspaces. Allocation peaks were essentially unchanged;
one p95 rose from 27.284 to 29.205 ms at 10 workspaces. No speedup is claimed for
empty gate sets. SQLite timings vary with VM scheduling, are not PostgreSQL SLOs,
and allocation peaks are not process RSS. No live provider, network, or native
device performance is measured.

## Verification

- Before edits: 57 relevant backend tests passed. Investigation also ran 135 runner
  Git tests (14 subtests) and 15 web Git graph tests, all passing.
- Five new regression cases failed on the original implementation, then passed
  with the change. They cover exact ID-only projection/no ordering, duplicate IDs,
  scoped/resolved/missing gates, empty scope/no SQL, root/child propagation and
  fresh reads after answering/rejecting gates. No timing thresholds in tests.
- After edits: **193 passed** across attention, conversations, conversation
  service/reads, permission queries/flow, harness API, pending interaction reads,
  and MCP chat tests. Existing API tests emitted async task teardown/unraisable
  warnings; this run does not establish that those unrelated tests are leak-free.
- Ruff passes for repositories, new tests and benchmark; formatting and
  `git diff --check` pass. The service's full Ruff check reports a pre-existing
  89-character line at line 149 (also present in the baseline); other rules pass.
- No UI code changed, so frontend/device builds and visual interaction checks were
  not required. REST and MCP correctness was exercised through their existing tests.
