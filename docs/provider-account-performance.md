# Provider-account read batching

Baseline: `f9eb24b098f89774d623046a63a3bef823449f56` (`origin/dev`). Both
AGENTS files were read, branches checked, the clean worktree stashed (nothing
to save), and `dev` fetched/reset before code inspection. Existing performance
branches and recent history/Git/plugin/resource optimizations were checked.

## Investigation and selection

The investigation covered backend repositories and services, runner file and
process operations, Vue chat/file rendering, native chat snapshots, and shared
stream reducers/client code. Six plausible candidates were ranked by impact,
path frequency, measured confidence, implementation risk, and architectural fit:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend workspace-list relation reads | Authenticated REST with 1/10/30 workspaces, even without images or attachments: 9/27/67 queries; 10.5/22.7/49.6 ms p50. Presentation bypasses prefetched credentials and reads plugins per workspace. Frequent UI path, strong evidence; completing projection batching requires care across REST/service boundaries. |
| 2 | Backend provider-account resolution | Full service read with 1/10/30 shared accounts: 15/33/73 queries; 12.7/33.5/82.3 ms p50. Membership and secret reads repeat per account, account visibility is loaded twice, and selections are queried per provider. Settings and model/turn resolution use this path; localized batching is feasible but must retain binding checks and locks. |
| 3 | Runner upload archive processing | Real gzip/tar conversion of 200 synthetic source files (42,251 compressed bytes; 11,376,640 tar bytes): 50.4 ms p50, 60.5 ms p95, and 50.4 ms p50 event-loop stall over 15 samples after two warmups. Conversion runs synchronously inside async upload; less frequent than read paths, but stalls unrelated runner traffic. |
| 4 | Shared streaming part scans | `closeRunningStreams` and `findPart` scan the active message's parts on text/reasoning deltas. Frequent, potentially costly on long agent turns; source evidence only, with ordering/reconciliation risks. |
| 5 | Mobile streaming publication | The active message's parts are cloned on deltas, and session snapshots rebuilt on publication. Historical frozen messages already share structure, limiting the opportunity. Needs native render/allocation profiling and careful immutable snapshot semantics. |
| 6 | Web markdown streaming | Full-document parse, sanitization and DOM parsing recur as rendered text changes. Existing updates are throttled; incremental rendering needs reference-link, media and security characterization. Source evidence only. |

The first three formed the shortlist **before** an OS-random draw using
`secrets.randbelow(3)`. It selected **provider-account-resolution**. There was
one successful draw and no redraw. Exploratory timings above are separate
from the controlled comparison below.

## Change and boundaries

`ProviderAccountService.list_accounts` now loads visible accounts once and
shares that request-local list with selection resolution. All selections load
in one repository read, and authorization reuses the membership already locked
inside the provider-turn fence. The unused single-selection reader and repeated
admin lookup were removed.

`CredentialSvc.get_provider_secrets` obtains bound encrypted values in one
repository query. Both single and batch reads share the live account/provider/
credential scope predicate and JSON decoder. ORM aliases keep reverse-relation
predicates on the same account join, so a valid sibling cannot legitimize an
invalid binding. Batch reads also enforce the requesting organization and
personal/shared visibility. Expected missing bindings still fail closed.

The existing transaction, admission/revocation mutex, locked active membership,
first-selection ordering, `get_or_create` initializer, explicit empty choices,
revocation semantics, masking and response schemas remain intact. Filtering one
provider still initializes other providers, without decrypting their secrets.
ORM remains in repositories; credential decryption remains in the credential
service. There are no caches, dependencies, migrations, protocol changes or UI
changes. State is retained only for the current request.

## Measurements

```bash
backend/.venv/bin/python scripts/benchmark_provider_accounts.py
backend/.venv/bin/python scripts/benchmark_provider_accounts.py \
  --backend-root /path/to/archived/backend
```

Python 3.12.3, Django 5.2.17, SQLite 3.45.1, Linux. The same script runs on
disposable file-backed databases with fixed IDs/timestamps, five providers,
alternating personal/shared accounts, and a member caller. REST covers Django
middleware, API-key authentication, repository/service work, decryption, schema
validation and JSON serialization. Selection service reads are measured too.
First-selection writes are warmed before timing.

Each implementation/workload has three warmups and 31 measured samples, repeated
in two rounds with reversed baseline/optimized execution order. No test suite
ran during these measurements. Query counts and `tracemalloc` peaks are collected
separately from latency. p95 uses nearest rank. The second round:

| Accounts | REST queries before → after | REST p50 ms before → after | REST p95 ms before → after |
| ---: | ---: | ---: | ---: |
| 0 | 17 → 12 | 16.334 → 12.157 | 20.962 → 16.510 |
| 1 | 19 → 13 | 17.874 → 13.588 | 20.286 → 19.455 |
| 10 | 32 → 13 | 36.106 → 15.848 | 39.555 → 17.405 |
| 30 | 62 → 13 | 80.774 → 18.961 | 92.766 → 21.355 |

At 30 accounts this is **76.5% lower p50**, **77.0% lower p95** and **79.0%
fewer queries**. The first round independently measured 100.033 → 24.701 ms
p50 and 124.318 → 29.537 ms p95. At 1/10 accounts the second-round p50 gains
are 24.0%/56.1%. Selection reads use 12 → 8 queries; their 30-account p50
was 11.035 → 8.262 ms in round two.

For 30-account REST reads, peak traced Python allocations were 330,243 →
229,017 bytes (30.7% lower); these are allocation peaks, not process RSS.
Every complete REST and selection response hash matches across both revisions
and rounds. There are no runner/provider calls or network latency in this
benchmark. Production latency and contention gains are not measured here;
PostgreSQL correctness and concurrent initialization were tested separately.
Raw results: [benchmarks/provider-accounts.json](benchmarks/provider-accounts.json).

## Verification

- Before editing: 49 targeted provider/account/catalog/credential tests passed.
- The new query-growth test fails on the archived baseline (19 versus 62
  queries at 1 versus 30 accounts) and passes with batching.
- Regression coverage checks constant query budgets, account visibility and
  masks, provider filtering, malformed bindings including shared reverse joins,
  deleted accounts, invalid JSON, secret rotation, inactive users, empty and
  disconnected accounts, concurrent first reads and explicit/revoked empty
  choices observed during initialization.
- Relevant SQLite suite: **415 passed, 2 skipped** (the skips require
  PostgreSQL). Command: `cd backend && .venv/bin/pytest -q apps/credentials/tests
  apps/harness/tests/test_provider*.py apps/harness/tests/test_models_catalog*.py
  common/tests/test_adapter_boundaries.py`.
- Final new regressions plus architectural boundary checks: **49 passed** on
  SQLite, including the two newly added empty/concurrent-read cases.
- Isolated local PostgreSQL 16: **45 passed** across account service/REST,
  query/binding regressions, revocation signals and real SSE turn tests; the
  final regression-only run was **16 passed**, including concurrent first reads.
  The 45-test run emitted a database-teardown warning about six lingering
  connections from the SSE suite; the final 16-test run tore down cleanly.
  The disposable cluster was stopped after verification.
- Ruff lint passed for all changed Python files using
  `--config backend/pyproject.toml`; new tests and benchmark passed formatting.
  Two existing formatting differences elsewhere in the repository modules
  were preserved. `git diff --check` passed. No baseline test failures occurred.
- Final diff reviewed for scope, repository/service boundaries, secret handling,
  retained state and concurrency. No UI behavior changed, so browser/device
  validation was not applicable.

The other five candidates remain unchanged.
