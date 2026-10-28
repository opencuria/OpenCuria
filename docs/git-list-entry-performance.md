# Git repository list: batch HEAD reads

Baseline: `922f3e2` on the fetched `origin/dev`, 2026-10-28. Both AGENTS files
were read, all branches checked, and the requested stash/fetch/switch sequence
completed before code inspection. The worktree was clean. Existing performance
branches had no unmerged commits relative to dev.

## Investigation and selection

Reviewed backend repositories/services, runner Git/file/metrics execution,
web Markdown rendering and chat stores, mobile transcript/snapshot updates,
and shared chat/client helpers. Existing plugin batching, provider-history,
stream persistence, mobile message sharing and Git snapshot optimizations were
excluded. Candidates were ranked by impact, path frequency, measured evidence,
implementation risk and architectural fit:

| Rank | Candidate | Evidence and risk assessment |
| --- | --- | --- |
| 1 | Runner file-read response parsing | Real 5 MiB `read_file`: 48.111 ms p50; existing split/join parser alone: 14.725 ms. At 25 MiB: 217.989 / 93.611 ms. Frequent media/file path; localized format-preserving change possible. |
| 2 | Backend message-edit discovery | Two real ORM discovery reads materialize 50 MiB for 200 messages: 51.037 ms p50 / 51.387 ms p95. Only role and IDs are consumed. User-visible admission; ordering/scoping/concurrency need care. |
| 3 | Runner Git list-entry round trips | Five repos require 28 guest executions, including 10 separate branch/HEAD reads. Local scout: 43.428 ms p50. One fewer SSH channel per repo offers predictable work elimination; narrow read-only scope. |
| 4 | Mobile snapshot metadata cloning | Sessions, lineage, todos and gates are cloned/frozen on every publication, including streamed text. Message parts already share unchanged rows. Device profiling and ownership work needed. |
| 5 | Web full-document Markdown parsing | Parsing, sanitizing and DOM reconstruction repeat on growing output. Already coalesced at 50 ms; browser profiling needed and incremental parsing has correctness risk. |
| 6 | Shared descendant-session traversal | Repeated full session scans in both clients; typical shallow trees may be cheap. Preserve traversal/event semantics. |
| 7 | Runner file-write chmod round trip | 64 KiB writes: 5.635 ms p50; chmod alone: 1.281 ms. Encoding mode in tar alone would change umask behavior, weakening the risk/benefit ratio. |

An actual `secrets.choice(top_three)` draw using OS randomness selected rank 3.
There was one draw and no redraw. The selection and scout measurements are in
[`benchmarks/git-list-entries/`](benchmarks/git-list-entries/). Scout timings
diagnose candidates; they are not the paired before/after measurements below.

## Change and boundaries

`GitService._git_list_entry` now runs the same two `git rev-parse` commands in
one guest execution. Each command keeps its own exit status and output.
Detached and unborn HEADs and optional-read failures keep their original
field values; missing Git still produces the structured `missing_git` error.

The existing snapshot framing implementation moved from `git_snapshot.py`
to `git_reads.py`. HEAD and snapshot batches share one shell framing function
and one parser. Snapshot batches retain their primary-error early exit;
HEAD batches always return both read outcomes. The random boundary stays a
literal argv argument, and incomplete/ambiguous frames fail closed. Existing
snapshot error text is preserved. All imports and consumers were updated;
there is no compatibility facade or second framing implementation.

This is runner execution only: no agent/provider/permission/business decisions
move layers. Existing containment checks, per-repository locks, ordering,
timeouts, cancellation and credential sourcing remain in their existing owners.
There are no new dependencies, caches, background workers or retained state.
REST/Socket.IO result schemas and frontend behavior are unchanged.

## Measurements

`scripts/benchmark_git_list_entries.py` extracts the original list-entry method
from Git and compares it with the current method, sharing unchanged discovery
and orchestration. Each workload uses identical disposable committed repos,
three warmups, seeded-shuffled before/after pairs, and exact equality of the
complete results. Commit hashes and branches are checked outside timing.
Python allocation peaks are separate tracemalloc probes, not latency samples.

Before production edits, both versions used 8/28 total calls for 1/5 repos,
and 2/10 calls in the isolated HEAD phase. Pre-edit local and SSH reports
establish the benchmark baseline independently of the optimized code.

The complete SSH measurement runs the public `list_repos` operation, including
discovery, root/metadata containment and repository locks. Actual find,
realpath, shell and Git commands execute through production
`QemuRuntime.exec_command_wait` on a persistent, host-key-verified loopback
OpenSSH connection. The runtime maps `/workspace` to the disposable fixture
tree and maps returned paths back. Host credential sourcing is disabled for
synthetic fixtures; production sourcing is covered by execution tests.

There is no simulated delay. Connection setup, QEMU, remote network latency,
Socket.IO transport and browser rendering are excluded. These are local
integration measurements, not production latency guarantees.

Initial complete SSH run, 41 pairs per case:

| Repos | Total guest calls before → after | p50 ms before → after | p95 ms before → after |
| --- | --- | --- | --- |
| 1 | 8 → 7 | 41.644 → 37.711 | 52.476 → 50.629 |
| 5 | 28 → 23 | 149.325 → 130.605 | 189.263 → 172.792 |
| 15 | 78 → 63 | 477.565 → 501.073 | 990.096 → 898.311 |

At five repos, the complete request improves p50 by **12.5%** and p95 by
**8.7%**. The initial 15-repo median regresses amid large scheduling variance;
it does not establish an overall latency win at that size. A separate repeat
with 61 pairs and raw latency samples confirms the overall improvement:

| Repos | p50 ms before → after | p95 ms before → after | Median saving within each pair |
| --- | --- | --- | --- |
| 5 | 176.203 → 135.334 | 348.698 → 331.917 | 25.566 ms |
| 15 | 467.267 → 418.597 | 990.914 → 807.138 | 77.687 ms |

The repeat improves p50 by **23.2% / 10.4%** at 5/15 repos. Absolute times
vary substantially on the shared host; neither result justifies promising
the same percentage on a production VM. Both full-list runs improve the
five-repo median, while the isolated phase identifies the eliminated cost.
Complete repeat Python peaks are 391,289 → 391,268 bytes at five repos and
447,757 → 461,039 bytes at fifteen repos. Raw samples and all earlier results
are retained, including the initial 15-repo regression.

The isolated SSH HEAD phase (41 pairs) improves at all measured sizes:
1 repo: 15.310 → 10.352 ms p50; 5 repos: 56.375 → 37.064 ms;
15 repos: 161.870 → 107.427 ms. This phase halves its guest calls. At five
repos its p95 is 68.943 → 46.931 ms, and peak Python allocations are
341,031 → 311,005 bytes.

Local complete-list tests without SSH show no consistent latency benefit:
the repeat's 1/5/15-repo p50 is 7.080/35.012/104.720 ms before and
7.693/35.590/110.554 ms after. The local test runtime also uses different
execution mechanisms for individual and batched Git commands. These results
do not demonstrate a CPU speedup. The target is avoiding production SSH
channels/round trips, not reducing the number of Git subprocesses.

In the initial full SSH probe, five-repo peak Python allocations are
390,800 → 390,326 bytes; fifteen-repo peaks are 446,624 → 458,724 bytes.
These small single-call differences do not establish process or guest RSS.
The implementation adds no retained resources or concurrency.

Reproduce complete local and SSH comparisons from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_git_list_entries.py
runner/.venv/bin/python scripts/benchmark_git_list_entries.py \
  --ssh-port PORT --ssh-key KEY_PATH --known-hosts HOSTS_PATH
```

Add `--entries-only` to the SSH invocation to isolate the HEAD phase.
All fixtures are deleted on exit. The temporary SSH server and keys are
workspace-only verification setup, not application dependencies.
The temporary server was stopped and its keys removed after measurement.

## Verification

- Before production changes: existing Git operation tests: 123 passed plus
  14 subtests. Runner file/path/chunk/archive/capture scouting tests: 152 passed
  plus 12 subtests. Backend fork/edit/copy scouting tests: 81 passed, 1 skipped.
  No pre-existing failure was found; backend tests emitted existing deprecation,
  missing-static-directory and pending-task warnings.
- Five tightened call-budget cases failed on the original implementation.
  Ordinary repos now require `4N + 3` calls instead of `5N + 3`; a workspace-root
  repo requires five instead of six. No containment probe was removed.
- Final runner checks: **342 passed plus 14 subtests**, covering all Git tests,
  capture fences, SSH ownership/exit status and WebSocket interfaces.
- Backend Git service/REST/socket/permission and MCP adapter checks:
  **161 passed**, with existing Django/Ninja/static-directory warnings.
- Web Git store/API/panel consumers: **70 passed**. Root `npm run type-check`
  passed for webapp, mobile, shared client and computer-use package.
- New cases cover actual committed/detached/unborn/non-repository reads,
  Unicode/shell-syntax branches, literal markers, credential sourcing with
  `set -e`, optional failures, missing binaries, malformed/truncated frames,
  shell failures, fresh HEAD after checkout, timeout and cancellation while
  another list waits for the same repository lock. Snapshot framing/security
  tests remain green.
- New/renamed files and updated small test modules pass Ruff. Existing
  `git_service.py` and `test_git_operations.py` have the same 7/8 baseline lint
  diagnostics, with no new diagnostics. `git diff --check` passes.
- No UI code changed. Real Git/SSH execution and client contracts were tested;
  a visual screenshot would not demonstrate this executor optimization.

The other promising candidates remain unfixed: runner file-read parsing,
backend message-edit body loading, and mobile snapshot metadata cloning.
