# Git discovery: reduce guest process amplification

## Investigation and selection

Baseline: `7a91ca1a303a1033ab07757f2f5f78ae978dc4a2` on `origin/dev`.
Read both AGENTS files, checked all local/remote branches, and ran
`git fetch origin && git switch -C dev origin/dev` before inspecting code.
The worktree was clean; no user changes needed stashing. Existing request-lifecycle,
web-resource, lazy-timeline, Git lazy-loading and harness-history improvements
were excluded. Investigation covered backend, runner, webapp, mobile and shared
packages. Ranked by impact, frequency, evidence, risk and architectural fit:

| Rank | Candidate | Evidence | Scope / risk |
| --- | --- | --- | --- |
| 1 | Backend plugin runtime snapshot | Real SQLite: 1/5/20 effective plugins produce 6/18/63 builder queries; full run preparation p50 9.1/18.6/66.6 ms | Per-run path; low/moderate ordering and credential-isolation risk |
| 2 | Runner Git summary discovery | 1/5/15 sibling repos require 11/43/123 guest execs; visible Git tab polls every four seconds | Frequent; moderate path-security risk; no need for concurrency or caching |
| 3 | Shared busy transcript merge | Actual reducer: one active assistant with 1,000/3,000 parts takes roughly 12–15/95–99 ms p50; ordinary distributed 3,000-part history is only 0.37 ms | Reconnect/refresh path; placeholder/order semantics and native immutability |
| 4 | Mobile completion reconciliation | Idle and conversation-change handlers both force timeline/todo refreshes | Source evidence; must preserve reconnect invalidation |
| 5 | Backend streamed-text persistence | Each delta reads/updates a part and updates accumulated message content | Source evidence; batching risks durability/cancellation behavior |
| 6 | Mobile streaming snapshot publication | Transcript-array copies and target-message part cloning on accepted deltas | Source evidence; requires device profiling and immutable snapshots |
| 7 | Web/mobile streamed Markdown processing | Growing source is reprocessed on throttled updates | Needs incremental syntax/device verification; not selected |
| 8 | QEMU image-cache setup | Synchronous download and subprocess helper called directly from async image build | Source evidence; cold path, cancellation/worker lifecycle risk |

The top three were fixed before an OS-random draw using Python
`secrets.randbelow(3)` at `2026-10-24T05:11:36.606443+00:00`. Index **1** selected
runner Git discovery. No redraw was needed. Other opportunities remain unfixed.

## Change and boundaries

`runner/src/services/git_service.py::_git_verify_repo_root` used three separate
`git rev-parse` processes for top-level, gitdir and common-dir, and two realpath
processes even when both metadata paths were identical. Every discovered candidate
paid this cost before its lightweight branch/HEAD reads.

- Read the three paths with one fixed argv:
  `git rev-parse --path-format=absolute --show-toplevel --absolute-git-dir --git-common-dir`.
- Require exactly three single, absolute paths and the expected repository root.
  Reject malformed or unsuccessful probes before any metadata/data commands.
- Resolve each distinct raw metadata path with the existing containment helper.
  Ordinary repositories share one path; linked worktrees still check both.
- Remove the obsolete separate metadata-probe implementation. Both discovery and
  explicit Git operations continue to use the same root-verification method.

No cache, retained trust, new dependency, migration, parallelism, background work,
protocol change or agent/business decision is introduced. Git's already-required
`--path-format=absolute` option was also used before this change. Credential sourcing,
error redaction, containment, deterministic ordering, repository locks, timeouts and
cancellation stay in the existing executor path. No backend/frontend adapter changes.
This does not claim to eliminate all Git polling overhead: discovery and the two
branch/HEAD reads remain sequential.

For ordinary sibling repositories, total `list_repos` calls fall from **8N + 3**
to **5N + 3**. At 15 repositories this is **123 → 78 (36.6% fewer execs)**, or
675 fewer guest execs/minute for one continuously visible client polling every four
seconds. That rate is an arithmetic maximum, not observed production traffic.
Distinct worktree metadata needs one additional realpath per worktree. Explicit
operations also benefit from the shared verification method.

## Measurement

### Real process integration

The real baseline/current `GitService` executed every argv using actual Git 2.43.0,
find, realpath and shell binaries in a disposable chroot. No fake filesystem probes,
mounts, credentials, network, external service or artificial delay. Python 3.12.3;
5 warm-ups and 51 timed samples per version/case, alternating baseline/current order
against identical fixtures; p95 is nearest rank. Full response SHA-256 digests matched.

| Repos | Execs before → after | p50 ms before → after | p95 ms before → after |
| ---: | ---: | ---: | ---: |
| 1 | 11 → 8 | 28.912 → 20.788 | 42.090 → 30.318 |
| 5 | 43 → 28 | 168.619 → 106.243 | 427.171 → 259.227 |
| 15 | 123 → 78 | 458.263 → 278.074 | 1130.754 → 708.027 |

At 15 repositories: **39.3% lower p50, 37.4% lower p95**. These are local process
measurements including one chroot/shell launch per runtime call, not actual QEMU,
QEMU/SSH or production latency. Host scheduling causes noisy tails: an earlier
15-sample run had 15-repo p50 382.272 → 215.632 ms but p95 469.157 → 472.099 ms,
and 5-repo p50 was essentially flat. The larger alternating run above improved all
paired p50/p95 cases; the exact eliminated-call count is deterministic.

Live behavior checks also compared `list_repos` and explicit `working_diff` responses
for committed/unborn/detached states, internal linked worktrees, nested submodule
metadata, staged/unstaged/untracked files, shell-hostile paths, and external gitdir /
symlink rejection. Digests matched and injection marker files remained absent.

### Portable reproduction

From the repository root, using only local synthetic repositories:

```bash
runner/.venv/bin/python scripts/benchmark_git_discovery.py --tracemalloc
# Run exactly the same script/interpreter against an archived baseline:
runner/.venv/bin/python scripts/benchmark_git_discovery.py --repo /path/to/baseline
```

This portable harness uses the existing `FakeGitRuntime`: Git commands execute real
local Git; find/realpath map the synthetic workspace using the test helper. Its
optional 5 ms/call delay is **simulated**, not a QEMU/SSH measurement. It uses fixed
commit identities/dates and isolated Git config, checks response-digest stability,
and keeps query/call accounting and optional allocation tracing outside latency.
31 warmed samples per scenario; repeated before/after results:

| Repos | Delay/call | p50 ms before → after | p95 ms before → after |
| ---: | ---: | ---: | ---: |
| 1 | 0 ms | 31.138 → 21.010 | 71.509 → 31.242 |
| 5 | 0 ms | 76.819 → 26.094 | 132.052 → 111.383 |
| 15 | 0 ms | 249.007 → 89.052 | 341.195 → 106.577 |
| 1 | 5 ms simulated | 96.153 → 73.930 | 109.568 → 76.020 |
| 5 | 5 ms simulated | 351.898 → 247.290 | 386.515 → 276.880 |
| 15 | 5 ms simulated | 904.354 → 591.216 | 1021.963 → 775.122 |

All response digests and call counts matched the first measurements. At 15 repos /
zero delay the separate Python allocation probe was 156,307 → 132,820 bytes. This
includes test-runtime call recording; it is not production RSS or a memory guarantee.
All fixtures, temporary homes/databases and the live chroot were removed on exit.
Raw results, live reproduction script and selection record are workspace artifacts
under `/workspace/reports/performance-investigation-20261024/`.

## Verification

- Before source edits: existing Git tests **122 passed / 14 subtests**. Full runner:
  **812 passed, 7 skipped, 33 subtests**, and **four pre-existing failures** in
  `test_codex_managed.py::test_actual_backend_codex_argv_passes_production_stream_boundary`
  (all provider/readonly combinations expect a missing `multi_agent=false` flag).
- Five new process-budget cases failed on the old implementation before the fix.
- Final Git tests: **141 passed / 14 subtests**. Nineteen new cases cover process
  budgets, root/unborn repositories, malformed combined outputs, distinct worktree
  paths, external common-dir rejection, timeout/cancellation, and rechecking replaced
  metadata across polls. Existing path security, mutations, locks, auth/redaction and
  Socket.IO error/cancellation tests remain green.
- Full runner after implementation: **831 passed, 7 skipped, 33 subtests** and the
  **same four Codex failures**; no new failures.
- Backend Git service/REST/Socket.IO/MCP/permission tests: **143 passed**.
- Web Git store/API/panel consumers: **67 passed**. Root `npm run type-check` passed
  for webapp, mobile and shared client.
- New files pass Ruff lint/format. Existing modified modules have no new lint
  diagnostics (175 before, 173 after). Python compilation and `git diff --check` pass.
- Rebased without conflicts onto incoming `2f5075a` (Claude upgrade) and `a00984a`
  (mobile chat UI), preserving both commits. Neither touched Git implementation or
  contracts. Re-ran the full runner suite: **840 passed, 7 skipped, 33 subtests**,
  with the same four existing Codex failures. Backend Git **143**, web Git **67**,
  root type-checks and a post-rebase process-budget/digest smoke check passed again.
- Independent architectural/security review found no blocking issue. No live UI
  screenshot is included: this is executor I/O elimination, with no visual change;
  actual Git execution and client contracts were verified instead.
