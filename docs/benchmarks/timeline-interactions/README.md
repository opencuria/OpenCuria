# Pending interaction reads

## Selection and scope

Baseline: `7721d974` on `origin/dev`. The working tree was clean before
fetching/resetting `dev`. Existing performance branches were already contained
in `dev`. Reference repositories were not modified.

The investigation covered backend repositories/services, runner file/git/metrics
paths, webapp stores, mobile snapshots, and shared chat/client packages.

| Rank | Opportunity | Evidence and impact/risk assessment |
| --- | --- | --- |
| 1 | Timeline pending gates | Both gate readers independently validate the session, traverse descendants, and deserialize every session for names. Timeline opening/idle refresh and MCP polling pay this repeatedly. High confidence; small service/repository change. |
| 2 | Mobile streaming snapshots | `ChatController.buildSnapshot` clones/freezes all session headers and lineage on every publication, including token deltas. Frequent path; identity preservation and native UI verification make this riskier. Not benchmarked here. |
| 3 | Codex checkpoint CPU work | Synchronous base64 validation, hashing, and JSON processing run in async capture/restore/load/save paths, with up to 50 MiB of state. High impact on concurrent chats; a complete solution needs more than thread offload. |
| 4 | Credential removal N+1 | `PluginService.credential_removal_gaps` reads requirements per effective plugin. Clear query amplification, but an infrequent settings operation. |
| 5 | Shared sidebar ordering | Conversation grouping sorts the entire feed and parses dates during comparisons. Shared by web/mobile; significance still needs profiling with large feeds. |
| 6 | Shared Git ordering | `enforceChildBeforeParent` repeatedly splices/reindexes malformed ordering. Potentially expensive, but normal Git input is already topological. |

Runner inspection found existing bounded/concurrent Git reads and concurrent VM
sampling, so those were not selected as new bottlenecks.

Actual `secrets.choice` draws from the top three selected Codex checkpoints
first, then timeline gates from the remaining two. The first candidate was
rejected before any production edits: five validations of a valid 32-MiB
checkpoint blocked a 1-ms asyncio heartbeat for 177–190 ms. `asyncio.to_thread`
still left 86–94 ms stalls (GIL-held base64 work); validation itself took
176–189 ms inline versus 186–200 ms threaded. JSON processing would also need
attention. A complete, simple fix was not established within this scope.

## Implementation

`InteractionService.list_pending_interactions` selects the session tree once,
reads both pending request types, and attributes only sessions with gates.
`HarnessSessionRepository.agent_names_by_ids` selects just IDs and agent names;
empty gate sets skip that query. REST timeline presentation and MCP pending
inputs use the same service method. The superseded single-type methods were
removed after migrating every caller.

No cache, dependency, migration, transaction or execution change was added.
Scope validation, permission display redaction, per-type ordering, question
projection, and external response shapes remain unchanged. Attribution and
descendants are read afresh on each request. As before, this read is not an
atomic snapshot across concurrent gate updates.

## Measurement

`scripts/benchmark_permission_lookups.py` now supports tree depth/width and
creates pending questions as well as permissions. The same script/fixtures
were used against original and optimized backend sources. Each run creates
and removes its own SQLite database; no existing data or external service is
used. Measurements use the authenticated Django HTTP test client, real ORM,
authorization and JSON serialization, with `DEBUG=False`.

The two workloads have 20 messages and 10,000 resolved permission rows:

- Root: one session, one permission and one question in the selected chat.
- Tree: 85 sessions (width four, three child levels), 85 permissions and two
  questions in the selected tree. Other workspaces also contain gates, testing
  that unrelated gates stay excluded.

Three warmups precede 101 timed requests. Query capture, response hashing and
one tracemalloc allocation measurement happen outside timed samples. The four
final runs were sequential, without simultaneous test/benchmark processes.
Full samples and response hashes are in the adjacent JSON files. An earlier
51-sample run was exploratory and is not used in the final comparison.

| Workload | SQL queries | HTTP p50 | HTTP p95 | Peak Python allocation |
| --- | --- | --- | --- | --- |
| Root, 101 samples | 24 → 21 | 19.852 → 18.364 ms | 21.225 → 25.125 ms | 189,359 → 177,295 B |
| Tree, 101 samples | 30 → 24 | 33.872 → 25.290 ms | 37.231 → 26.857 ms | 481,798 → 372,722 B |
| Root, 301-sample repeat | 24 → 21 | 19.844 → 18.261 ms | 22.693 → 19.803 ms | 188,774 → 176,681 B |

The tree improves p50 by 25.3%, p95 by 27.9%, and peak allocation by 22.6%.
The first root run's higher p95 warranted investigation. A separate 301-sample
repeat, running the candidate before the baseline, improves root p50 by 8.0%
and p95 by 12.7%, with the same query reduction and response hash. Both runs
are retained: short local HTTP tails vary, and a universal p95 gain should not
be inferred from these results. No additional tests ran during either pair.

Reproduce from the repository root:

```bash
baseline_root=$(mktemp -d)
git archive 7721d974 backend | tar -x -C "$baseline_root"
backend/.venv/bin/python scripts/benchmark_permission_lookups.py \
  --backend-root "$baseline_root/backend" --history 10000 --samples 101 --tree-depth 3
backend/.venv/bin/python scripts/benchmark_permission_lookups.py \
  --history 10000 --samples 101 --tree-depth 3
```

Use `--tree-depth 0` for the root workload. Original and optimized response
hashes match for each workload. These are local integration measurements,
not production network latency or PostgreSQL results. Allocation peaks are
Python allocations, not RSS. Long transcripts dominated by message/part
serialization can see a smaller relative improvement.

## Verification

Before production edits: 35 interaction/timeline tests and 99 harness HTTP/MCP
tests passed. Existing warnings included Django/Ninja deprecations, missing
development staticfiles, and unawaited/pending background tasks in API fixtures.
An initial command named a nonexistent `test_codex_state.py`; the corrected
Codex limits/safety/session-store selection passed 36 tests during investigation.

New coverage checks query budgets at depths zero/one/three and widths one/six,
with/without gates; scalar name projection; payloads and ordering; sibling
exclusion; fresh names, resolved gates and newly spawned descendants; unknown
session errors; and MCP mixed child-gate presentation. Existing HTTP tests cover
authorization and descendant gate visibility. The architecture guard includes
the new service operation.

Final verification: **218 passed** (185 existing warnings) with:

```bash
cd backend
.venv/bin/pytest -q \
  apps/harness/tests/test_pending_interaction_reads.py \
  apps/harness/tests/test_interaction_service.py \
  apps/harness/tests/test_harness_api.py \
  apps/harness/tests/test_timeline.py \
  apps/harness/tests/test_paket6_features.py \
  apps/mcp_app/tests/test_chats.py \
  common/tests/test_adapter_boundaries.py
```

Ruff passed for all changed Python files using `backend/pyproject.toml`, and
`git diff --check` passed. Frontend/runner/device tests were not run because
their code and contracts did not change. No production database, external
provider, or live agent was used for verification.
