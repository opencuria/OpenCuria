# Skill selection without catalogue-wide reads

## Investigation and selection

Started from `origin/dev` at `71948fe`, after reading both AGENTS files, checking
all branches, fetching and resetting `dev`. The working tree was clean, so no
stash was needed. Existing performance branches were already merged.

The survey covered backend repositories/services, runner files/Git/metrics,
Vue chat/Markdown, mobile snapshots/list rendering, and shared chat/client code.
Candidates were ranked by expected impact, execution frequency, measured
evidence, implementation risk and architectural cost:

| Rank | Candidate | Evidence and value | Risk |
| --- | --- | --- | --- |
| 1 | Runner directory-list guest calls | Each listing makes sequential `realpath` and `find` calls. Real local subprocesses, 500 entries: 4.170 ms p50 / 4.330 ms p95, 21 samples after three warmups. Removing one guest/SSH round trip could improve interactive browsing. SSH/QEMU latency was not measured. | Medium: preserve path containment, external-path policy and errors. |
| 2 | Backend skill selection | Selecting three skills reads every visible ID. Exploratory p50 grew from 1.263 ms at 100 skills to 36.642 ms at 10,000, with 2.37 MiB traced allocations. Used for admission and subsequent prompts inheriting saved skills. | Low: restrict existing repository reads and retain service semantics. |
| 3 | Backend summary projection | Summary material loads duplicate aggregate message content alongside structured text. At 10/50 synthetic turns: 6.737/24.338 ms p50 and 2.31/10.98 MiB traced allocations. | Medium: preserve legacy no-parts fallback and final-reply selection without duplicating service rules in SQL. |
| 4 | Backend sequential MCP startup | `McpRuntime.setup` opens servers serially; code evidence only. Potentially substantial startup latency. | Higher: task affinity, cancellation, deterministic collisions and cleanup. |
| 5 | Web growing Markdown projection | Each projection parses, sanitizes and traverses growing text. | Needs browser profiling; preserve media, references and sanitization. |
| 6 | Mobile snapshot publication | Sessions are cloned/frozen and ancestry is derived during publication. Unchanged message parts already reuse immutable snapshots. | Needs device profiling; preserve native snapshot identity. |
| 7 | Shared descendant traversal / upload normalization | Repeated session-array scans and repeated whitespace scans respectively. | Code evidence only; bounded depth and transfer costs reduce expected benefit. |

Runner VM metrics serialization was also investigated and rejected: probes
already run concurrently. One actual OS-backed
`random.SystemRandom().choice(top_three)` draw selected index **1**, skill
selection. No redraw occurred. See
[selection metadata](benchmarks/skill-selection/selection.json).

## Change and boundaries

`SkillRepository` now shares one visibility predicate between catalogue reads
and requested-ID reads. `get_visible_by_ids` loads only ID, name and body for
requested, visible skills. `get_visible_ids` returns only requested, visible
IDs. Both discard the model's name ordering because the service preserves
request order. The old unscoped body read and whole-catalogue ID method were
removed, with all consumers updated.

`SkillService.resolve_bodies` validates all UUIDs first, then resolves bodies in
one scoped query. Missing and inaccessible IDs retain the same error. Duplicate
inputs still produce duplicate body fragments. `filter_available_ids` parses
normalized inputs before querying, retains original UUID spelling and order,
and uses no SQL when no valid IDs remain.

ORM access stays in the repository; normalization, error policy, ordering and
prompt formatting stay in the service. REST/MCP, runner, shared packages and
frontends need no changes. No cache, dependency, schema migration, background
task, protocol, transaction or lock was introduced. Per-call storage scales
with the requested selection. A single body query also removes the gap between
the former visibility read and unscoped body read; resolution still rechecks
visibility after filtering saved selections.

## Method and results

`scripts/benchmark_skill_selection.py` migrates a disposable file-backed SQLite
database and exercises real services and ORM with `DEBUG=False`. Catalogues
contain mixed personal/organization skills with approximately 2 KiB bodies.
Three skills are selected in reverse catalogue order. `inherited_selection`
calls the actual filter and body-resolution services used to prepare saved
selections; provider execution and other admission work are excluded.

An original-only run was recorded before implementation. The paired run loads
the original service/repository from `71948fe` and alternates original/current
calls against identical fixtures. Each workload uses five warmups and 31 timed
samples per variant. Output hashes must match across variants and samples.
SQL capture, query plans, output hashing, GC and allocation tracing are outside
timed regions. Raw results: [before](benchmarks/skill-selection/before.json),
[paired](benchmarks/skill-selection/paired.json).

Paired results for inherited selection of three skills:

| Visible catalogue | p50 before → after | p95 before → after | Queries | Peak Python allocation before → after |
| --- | --- | --- | --- | --- |
| 100 | 1.923 → 1.071 ms | 1.974 → 1.092 ms | 3 → 2 | 58.91 → 30.07 KiB |
| 1,000 | 10.792 → 1.203 ms | 12.972 → 1.734 ms | 3 → 2 | 261.67 → 29.92 KiB |
| 10,000 | 78.682 → 1.125 ms | 150.075 → 1.239 ms | 3 → 2 | 2,430.19 → 30.07 KiB |

At 10,000 entries, inherited selection is **69.9× faster at p50** (98.6% lower
latency), with **98.8% less peak traced Python allocation**. Body resolution
alone drops from 39.508 to 0.586 ms p50, 109.902 to 0.671 ms p95, and two queries
to one. SQL plans replace two owner/org index traversals plus temporary name
sorts with primary-key lookups for the requested IDs, without temporary sorts.
Empty selections perform zero queries instead of one for filtering.

These are warm local service measurements, not production database or complete
chat latency claims. Allocation peaks measure temporary Python allocations,
not process RSS. Production/PostgreSQL latency and concurrent throughput were
not benchmarked. Execution remains synchronous ORM work at the same existing
async-to-sync boundaries; no new scheduling or shared mutable state exists.

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_skill_selection.py \
  --baseline-ref 71948fe --output /tmp/skill-selection.json
```

## Verification

- Before implementation: existing skill and session-settings suites, **30 passed**.
- New coverage against original code: **five expected performance failures**,
  ten passes. It caught extra queries, catalogue-wide row loading and reads for
  empty/invalid selections.
- After implementation: **216 passed** across skills, session settings, native
  harness, Codex admission, Claude harness, MCP chats and adapter boundaries.
- MCP skill/context suite: **five passed**, including CRUD, visibility and
  organization permissions. Total post-change coverage: **221 passed**.
- Regression coverage counts rows returned by real SQL, not wall-clock time:
  a two-ID selection reads two skill rows despite 500 unselected skills. It also
  checks duplicate bodies, order, UUID spelling, malformed/missing/hidden IDs,
  changed bodies, moved ownership, deletion and personal cross-org visibility.
- Django `manage.py check`, changed-file Ruff lint/format and `git diff --check`
  passed. The broader skills-directory lint finds 13 existing issues in the
  untouched initial migration and `schemas.py`; they were left unchanged.
- Existing warnings concern deprecated Django constraints/Ninja tuples and
  absent collected static files. No test failures remain in the relevant suites.

Directory-list guest-call batching, summary-data projection and MCP startup
concurrency remain promising follow-ups; none was changed in this work.
