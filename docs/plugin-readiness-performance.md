# Workspace plugin readiness query batching

## Investigation and selection

Started at `origin/dev` commit `94c77213aba44fbaba542765d59bdf7368657aab`.
Read both AGENTS files in full, checked all local/remote branches, verified the
working tree was clean, then fetched and reset `dev` to `origin/dev` before
inspecting implementation code. No stash was needed. The existing performance
branches were already included in `dev`; recent runtime snapshot, catalogue,
Git diff, mobile streaming and Agent-S projection optimizations were reviewed.

The survey covered backend services/repositories, runner Git/file operations,
Vue chat/Markdown, native chat publication/list rendering and shared chat/client
code. Candidates were ranked by impact, execution frequency, evidence, risk and
architectural cost:

| Rank | Candidate | Current evidence and expected value | Risk / scope |
| --- | --- | --- | --- |
| 1 | Backend workspace plugin readiness | A real SQL probe at 15 plugins used 19 queries, 12.891 ms p50 and 15.627 ms p95. Requirements are read once per plugin, with an additional read per required OAuth binding. Used by MCP workspace inspection/plugin listing and workspace configuration validation. | High confidence; existing bulk requirements repository can be reused. Preserve tenant, ownership, activation and OAuth policy. |
| 2 | Runner directory upload conversion blocks asyncio | Five real `FileManager.upload_file` calls over a synthetic 2,000-file gzip tar (223,825 bytes compressed) measured 343.398 ms median call time and 342.465 ms median maximum heartbeat delay. The runtime was an async test double; archive conversion was real. | Large effect on all runner requests during uploads; thread offloading needs bounded work, cancellation and capture-fence coverage. |
| 3 | Runner untracked Git diffs | Full Git RPC using real temporary Git repositories: 50 new files required 55 guest commands and 103.904 ms p50 over seven samples. Adding an explicitly simulated 5 ms delay to each untracked diff yielded 383.164 ms p50. | Bounded concurrent reads could improve interactive diff loading; SSH channel budgets, memory, ordering and cancellation need care. |
| 4 | Runner Git repository summaries | `_git_list_entry` still runs separate sequential branch and HEAD commands on the frequently refreshed repository list. | Code evidence only; less total work than untracked diffs, and local command savings may be small. |
| 5 | Web growing Markdown projection | `HarnessMarkdown.vue::parseMarkdownRoot` parses, sanitizes and traverses the entire document as streamed text grows. | Code evidence only; needs browser profiling. Reference links, media and sanitization make incremental processing higher risk. |
| 6 | Mobile snapshot publication | `ChatController.buildSnapshot` copies/freezes sessions and todos and derives ancestry on each publication. Recent immutable part-copy improvements already remove the largest identified copy cost. | Needs device profiling; preserve immutable snapshots and native list identity. |
| 7 | Shared descendant traversal | `collectDescendantSessionIds` repeatedly scans the session array until no descendants are added. | Code evidence only; actual subagent depth is bounded, so typical benefit may be limited. |

The top three were fixed before one OS-random `secrets.randbelow(3)` draw chose
index **0**, backend workspace plugin readiness. No redraw occurred. Selection
metadata and exploratory measurements are in
[`benchmarks/plugin-readiness/selection.json`](benchmarks/plugin-readiness/selection.json).
Only the selected candidate was changed.

## Change and architecture

`PluginService._workspace_required_gaps` now handles the complete requested
plugin set. It loads requirements through the existing bulk repository method,
filters owned credentials once, and groups gaps by plugin. Both
`list_workspace_plugins` and `validate_workspace_plugin_selection` use this
single implementation. Validation retains deduplicated selection order; listing
retains plugin-name and requirement-key order.

`PluginMcpServerRepository.list_oauth_for_plugins` replaces the former
single-binding query. One read loads only IDs, plugin IDs, requirement keys and
URLs for plugins with required OAuth dependencies. Binding lookup includes both
plugin ID and requirement key. Empty sets and ordinary-only requirements issue
no OAuth-server SQL query. Large MCP argument/environment/header/resource JSON
fields are not loaded.

For a nonempty selected set, listing changes from `4 + N + R` queries to
**5 ordinary / 6 with OAuth**, where `N` is plugins and `R` required OAuth
bindings. Validation changes from `2 + N + R` to **3 / 4**, excluding the
caller-owned credential fetch. The benchmark includes that fetch in both
measurement scopes, adding one validation query.

All ORM access remains in repositories; credential and OAuth decisions remain
in backend services. Adapters, runner and frontends are unchanged. There is no
cache, dependency, protocol change, asynchronous work, retained state or new
transaction/lock. Bulk maps live only for the current call. Existing security
checks and all-server OAuth validation are preserved.

## Measurement method

`scripts/benchmark_plugin_readiness.py` migrates a disposable file-backed SQLite
database and calls the real public services and repositories with `DEBUG=False`.
Fixtures contain 0, 1, 5, 15 or 50 mixed global/org plugins, one required credential
per plugin and mixed workspace activation state. Ordinary cases use an attached
org token; OAuth cases use a real encrypted synthetic personal grant, registration
and two servers sharing each requirement. Both scopes include reading attached
credentials. No database, credentials, network, provider or runner outside the
disposable fixture is used.

Each workload has five warmups and 31 timed samples per run. Output hashing, SQL
capture, GC and allocation tracing run outside timed regions. The baseline uses
the complete archived backend from the starting commit with the same script and
Python environment. Warm local service latency measures database/Python overhead,
not production network latency or complete agent time to first token. Allocation
peaks measure temporary Python allocations, not retained memory or process RSS.

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_plugin_readiness.py --output after.json
backend/.venv/bin/python scripts/benchmark_plugin_readiness.py \
  --backend-root /path/to/archived/backend --output before.json
```

## Results

Final runs were sequential, without concurrent tests, in the order **baseline,
optimized, optimized, baseline**. Each variant pools 62 latency samples per
workload; p95 uses the nearest-rank percentile. All output SHA-256 hashes match
across all four runs. Raw samples, environment versions, query counts and
allocation peaks are in
[`benchmarks/plugin-readiness/`](benchmarks/plugin-readiness/); the pooled
comparison is in [`summary.json`](benchmarks/plugin-readiness/summary.json).

Complete `list_workspace_plugins` service calls:

| Credential type | Plugins | Queries before → after | p50 ms before → after | p95 ms before → after |
| --- | ---: | ---: | ---: | ---: |
| Ordinary | 0 | 4 → 4 | 2.840 → 2.812 | 3.836 → 3.285 |
| Ordinary | 1 | 5 → 5 | 3.529 → 3.992 | 3.608 → 4.192 |
| Ordinary | 5 | 9 → 5 | 6.284 → 4.299 | 6.756 → 4.509 |
| Ordinary | 15 | 19 → 5 | 13.415 → 4.944 | 18.209 → 5.420 |
| Ordinary | 50 | 54 → 5 | 36.953 → 7.128 | 41.842 → 8.509 |
| OAuth | 0 | 4 → 4 | 2.817 → 2.827 | 2.883 → 3.075 |
| OAuth | 1 | 6 → 6 | 4.438 → 4.388 | 4.576 → 4.954 |
| OAuth | 5 | 14 → 6 | 10.849 → 5.596 | 11.549 → 6.051 |
| OAuth | 15 | 34 → 6 | 26.638 → 8.556 | 27.330 → 9.467 |
| OAuth | 50 | 104 → 6 | 82.186 → 17.369 | 87.187 → 18.025 |

At 15 plugins, listing p50 falls **63.1% ordinary / 67.9% OAuth**; at 50 it
falls **80.7% / 78.9%**. Validation also benefits: at 15 plugins, ordinary
queries change **18 → 4**, p50 **12.774 → 4.524 ms**, p95 **14.298 → 5.923 ms**;
OAuth queries change **33 → 5**, p50 **26.279 → 7.983 ms**, p95
**27.331 → 9.460 ms**. Those counts include the credential fetch.

The singleton ordinary listing increases by **0.463 ms p50 (13.1%)**, with
unchanged query count; the bulk path performs additional query construction and
grouping. These measurements do not isolate how much of that small difference
comes from each operation or timing variation. No singleton-specific duplicate
implementation was added. Empty and singleton OAuth cases retain their query
counts and similar latency.

Bulk reads trade additional temporary memory for fewer database round trips.
Median traced listing peak allocation at 15 plugins changes from **114,524 →
118,344 bytes** ordinary and **126,059 → 138,656 bytes** OAuth. At 50 it changes
from **202,848 → 246,340 bytes** ordinary and **234,537 → 325,161 bytes** OAuth
(about **42.5 / 88.5 KiB** additional temporary allocation). The largest measured
increase is 108.3 KiB in 50-plugin OAuth validation. Maps are local to one call,
with no retained cache or background resource. No thread/event ordering or
concurrency mechanism changes.

## Verification

Before implementation, the existing plugin, OAuth and MCP adapter suites passed:
**158 passed, 1 skipped**. No pre-existing test failure was observed. Eighteen
new characterization cases passed against the original implementation. The
15-plugin query guards failed as intended before optimization: **19 versus 5**
ordinary queries and **34 versus 6** OAuth queries.

After implementation, all **29 new regression/characterization cases passed**.
Coverage includes 0/1/5/15/50 plugins, both public service paths, complete gap
payloads and ordering, deduplication, global/org visibility, inactive/unavailable
plugins, personal/org credential ownership, foreign/internal credentials,
required/optional dependencies, missing/mismatched OAuth servers, corrupt grants,
disconnection and registration mismatch. Repeated-call tests cover attachment,
requirement and server changes. Cross-plugin tests use identical requirement
keys with distinct OAuth endpoints and optional bindings.

The broader relevant integration check passed:

```bash
cd backend
.venv/bin/pytest -q -rs apps/plugins/tests apps/credentials/tests \
  apps/mcp_app/tests apps/runners/tests/test_capture_configuration.py \
  apps/runners/tests/test_workspace_credential_api.py \
  apps/runners/tests/test_workspace_credential_resolution.py \
  apps/runners/tests/test_service_reads.py \
  apps/harness/tests/test_mcp_hardening.py \
  common/tests/test_adapter_boundaries.py
```

Result: **392 passed, 2 skipped**. The two skips cover PostgreSQL nullable-join
locking and deferred-trigger behavior; this environment uses SQLite. Existing
Django/Ninja deprecation and missing-static-directory warnings remain. The final
additional OAuth binding-isolation case was also verified in the separate
29-case run. REST/MCP authorization, atomic workspace configuration, MCP error
handling and cancellation are covered by the integration suites.

Ruff lint/format checks and `git diff --check` passed. Review confirms that only
the selected readiness path changes, the old single-binding repository method
has no remaining callers, and no ORM calls or agent decisions were added to
adapters, runner or presentation code. No frontend/runtime code changed, so
frontend builds and browser/device interaction checks were outside this change.
