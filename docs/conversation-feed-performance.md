# Conversation feed: eliminate per-workspace engine reads

## Scope and selection

Baseline: `10f8fa630874079d7169de0bb67214c6bba7fc85` on `origin/dev`.
The worktree was clean before fetching/resetting to `origin/dev`. Existing
performance, lazy-timeline, chunked-transfer and runner-decomposition branches
were already merged; this does not repeat their work.

The investigation covered backend, runner, webapp, mobile and shared packages.
Candidates were ranked by measured impact, path frequency, confidence, complexity,
regression risk and architectural fit:

| Rank | Candidate | Evidence and impact | Scope/risk |
| --- | --- | --- | --- |
| 1 | Backend provider-history reconstruction | Every native turn; 100 synthetic turns produced 201 queries and 513 ms p50 in the initial DEBUG-enabled scout | Moderate: preserve tool ordering, compaction and memory bounds |
| 2 | Backend conversation feed | Sidebar/mobile refreshes and MCP; 50 workspaces produced 61 REST queries, including 50 redundant session reads | Low: reuse the existing service projection |
| 3 | Shared busy-transcript reconciliation | Repeated array searches; local Node snapshot merges took 40 ms p50 at 3,000 parts and 684 ms at 10,000 | Moderate: preserve placeholder matching and stale-snapshot behavior |
| 4 | Plugin-list serialization | Four component/activation reads per plugin in `PluginService._serialize` | Low/moderate, but less frequent settings path; source evidence only |
| 5 | Mobile streaming Markdown scans | Full-source repair and image-reference lexing on a 50 ms cadence; isolated Node scans of very large answers exceed a frame budget | Needs device/streaming verification; not a production profile |
| 6 | Runner file-read parsing | Base64 payload is split into every line and rejoined; local 5 MiB parser-only experiment showed about 28 ms vs 3 ms for header-only splitting | Low/moderate, but smaller end-to-end impact and newline semantics need care |
| 7 | Runner heartbeat liveness checks | Sequential process probes every 15 seconds can accumulate runtime/SSH round trips | Higher concurrency/lifecycle risk; source evidence only |

The top three were fixed before drawing with Python `secrets.choice` (OS
randomness) at `2026-10-23T20:27:01.427089+00:00`. It selected rank 2,
`backend-conversation-feed`. No redraw was needed. Other candidates are not fixed.
The scout numbers above are not comparable to the dedicated benchmark below.

## Change

`HarnessSessionRepository.list_for_workspaces` already retrieves the root sessions,
including `harness_id`, in one query. Both REST and MCP nevertheless reloaded all
sessions, including children, once per workspace to reconstruct that field.

- `backend/apps/harness/harness_service.py`: include the public `harness_id` in
  `list_conversations`, preserving the existing empty-value fallback to `native`.
- `backend/apps/harness/api.py` and `backend/apps/mcp_app/tools/harness_sessions.py`:
  consume the shared projection instead of building independent engine maps.

Only unnecessary work is removed. ORM remains in repositories, business projection
remains in the service, and adapters remain thin. No dependency, cache, migration,
shared mutable state, new asynchronous work or frontend change is introduced.
Authorization, ownership, root-only filtering, attention/unread behavior, ordering,
JSON field names and each adapter's existing timestamp encoding are unchanged.
Private engine state and connection identifiers remain excluded.

## Reproduce and results

From the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_harness_conversations.py
```

The script creates/migrates a temporary file-backed SQLite database and removes it
on exit. Each workspace has one root conversation and four child sessions, with
mixed native/Claude/Codex engines. It measures 31 warmed requests per adapter at
1, 10 and 50 workspaces, with `DEBUG=False`. REST uses the authenticated Django HTTP
test client, including auth, ownership and JSON serialization. MCP measures its
handler including membership/ownership/JSON, **not** transport or key middleware.
Query counts and `tracemalloc` peaks are captured separately from timings.

The same script and interpreter were run before/after the production edits:

| Workspaces | Adapter | Queries before → after | p50 ms before → after | p95 ms before → after |
| ---: | --- | ---: | ---: | ---: |
| 1 | REST | 12 → 11 | 13.013 → 13.262 | 17.424 → 19.646 |
| 10 | REST | 21 → 11 | 24.993 → 14.642 | 35.926 → 16.889 |
| 50 | REST | 61 → 11 | 68.330 → 26.031 | 121.790 → 28.375 |
| 1 | MCP handler | 10 → 9 | 8.504 → 7.656 | 13.270 → 9.559 |
| 10 | MCP handler | 19 → 9 | 18.420 → 9.192 | 22.494 → 11.472 |
| 50 | MCP handler | 59 → 9 | 60.148 → 18.452 | 75.142 → 20.146 |

At 50 workspaces, REST p50 dropped **61.9%**, MCP-handler p50 **69.3%**.
SQL count is now constant with workspace count: **11 REST / 9 MCP-handler** for
nonempty feeds. One-workspace latency is noise-level; no gain is claimed there.

Python allocation peaks at 50 workspaces were 608,538 → 610,512 B for REST
(essentially unchanged) and 655,012 → 577,214 B for MCP. These are transient Python
allocation peaks, **not** process RSS or proof of production memory savings.
All before/after response SHA-256 digests matched. Only equivalent UTC timestamp
spellings are normalized for cross-adapter digest comparison.

A second sequential run against an archived baseline and the optimized tree
confirmed the same query counts and payload digests. At 50 workspaces its REST p50
was 137.579 → 34.928 ms and MCP-handler p50 93.383 → 25.510 ms. Absolute timings
vary with shared VM scheduling; the eliminated queries are deterministic. These
synthetic SQLite results do not establish production PostgreSQL latency or SLOs.

## Verification

- Before production edits: 114 relevant harness/history/persistence/feed tests and
  12 existing REST/MCP Claude lifecycle tests passed; 50 reducer tests also passed.
- Query-count and service-projection tests failed against the old implementation,
  then passed after the optimization.
- Final relevant backend run: **270 passed**, including the full MCP test directory,
  harness API/engine contracts, history, persistence and conversation tests.
- Web conversation-store and shared reducer consumers: **73 passed**.
- Before pushing, rebased onto the three incoming web-settings commits through
  `79e0707`; they changed no backend files. Post-rebase feed/Claude REST/MCP checks:
  **43 passed**; web consumers: **73 passed** again. Root `npm run type-check`
  passed for webapp, mobile and the shared client.
- New tests enforce constant query counts at 1/10 workspaces; mixed engine values,
  fallback, child exclusion, ownership/membership boundaries, empty feeds, private
  state exclusion and REST/MCP parity. Existing unread, attention, ordering and
  permission tests remain green.
- New files and changed production harness modules pass Ruff; `git diff --check`
  passes. The existing MCP module has 26 lint diagnostics and the existing feed
  test has one long-line diagnostic, unchanged from baseline.
- API-test coroutine-cleanup warnings also reproduce on the archived baseline;
  no execution-lifecycle code is changed. An exploratory foreign-org REST check
  exposed an existing unhandled membership `NotFoundError`; that error-mapping
  issue is outside this performance change. MCP membership rejection is covered.

No live frontend or production database was exercised: this is a backend query
optimization with real ORM/HTTP integration coverage and no visible UI change.
