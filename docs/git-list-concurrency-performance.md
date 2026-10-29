# Git repository list: bounded parallel inspection

Baseline: `2cfab8a` on fetched `origin/dev`, 2026-10-29. Both AGENTS files were
read and local/remote branches checked before inspection. The worktree was
clean; existing stashes were preserved. Existing performance branches were
already merged. This extends the existing HEAD batching and discovery probe
optimizations by overlapping independent repository inspections.

## Investigation and selection

Reviewed backend services/repositories and timeline projection, runner Git,
file and metrics operations, web chat rendering/stores, mobile immutable
snapshots/rendering, and shared chat/client helpers. Candidates were ranked
by expected user impact, path frequency, measured evidence, regression risk
and architectural fit:

| Rank | Candidate | Evidence and assessment |
| --- | --- | --- |
| 1 | Runner file-read response parsing | A predecoded 100 MiB guest response costs 365.606 ms p50 and 368.909 MiB additional Python allocations in `read_file`; seven samples, separate tracing. Split/join parsing is a narrow candidate for large attachments and harness reads. Network and file reading excluded. |
| 2 | Backend patch projection | Generated added-file diff: 100k lines / 1.89 MB takes 233.400 ms p50; 500k lines / 9.89 MB takes 1,216.937 ms, seven samples. Repeated full scans delay persisted edit/write events. A single bounded scan needs careful preview equivalence tests. |
| 3 | Runner Git list serialization | Complete local list at HEAD: 1/5 repositories require 7/23 guest calls and 6.878/27.769 ms p50, three samples. Discovery and HEAD inspections are sequential across independent repositories. SSH integration below confirms meaningful latency. Concurrency and cancellation require explicit bounds. |
| 4 | Backend conversation attention | Loads full pending gate rows for IDs and repeats descendant traversal per session. Plausible database/graph cost; needs profiling on deep session trees. |
| 5 | Mobile block derivation | `MessageRow` sorts and rebuilds blocks for each immutable streaming parts update. Existing memoization/virtualization limits the scope; native rendering profiling is needed. |
| 6 | Shared descendant traversal | Repeated full-session scans run during mobile snapshot publication and in web scope calculations. Creation order usually makes this cheap; lower confidence for ordinary trees. |
| 7 | Web message block derivation | Computed blocks/page identities process whole messages. Existing stable part membership already avoids much streaming invalidation, so no new bottleneck was established. |

One `secrets.choice(top_three)` draw using OS randomness selected rank 3.
There was no redraw. The selection record and raw SSH measurements are in
[`benchmarks/git-list-concurrency/`](benchmarks/git-list-concurrency/).
Exploratory timings above only assess candidates; they are separate from the
paired before/after experiment.

## Implementation and boundaries

`runner/src/services/git_service.py` inspects candidate roots and reads list
entries in ordered batches of at most four tasks. One semaphore shared by
the `GitService` limits active repository inspections across requests/workspaces,
including the initial root and find probes. The existing askpass setup stays
outside that budget. Tasks waiting for an existing
repository lock do not occupy a semaphore slot.

Each repository still passes the same ordered realpath, root, gitdir and
common-dir checks. Unsafe candidates are skipped; the cap counts verified
roots, including the workspace root. HEAD reads retain their repository
locks. Results stay sorted, independent of completion order. Batches settle
all started tasks before surfacing the first error in input order; cancellation
reaches all batch children. Existing per-command timeouts and SSH cancellation
semantics remain unchanged.

This is execution orchestration in the runner. It adds no agent, permission,
provider or business decisions. Backend/service/repository boundaries and
REST/Socket.IO/MCP payloads remain unchanged. There are no new dependencies,
caches, retained result state or background workers.

## Measurement

Reproduce from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_git_list_entries.py \
  --baseline 2cfab8a --repos 1 5 15 --samples 41 --loopback-ssh
```

The complete public `list_repos` operation runs through production
`QemuRuntime.exec_command_wait`, a persistent loopback AsyncSSH connection,
and actual local find/realpath/shell/Git processes. The benchmark creates
disposable committed repositories, maps `/workspace` paths to them, and
removes fixtures and the SSH listener on exit. Ephemeral client/host keys
authenticate the connection; host credentials and Git configuration are
excluded. Production credential sourcing remains covered by existing tests.

The baseline loads the original discovery, list orchestration and entry
methods from Git. Before production edits, a 21-pair control run measured
five repositories at 105.571/105.361 ms and fifteen at 297.156/293.637 ms
(the two implementations were identical). The final experiment uses three
warmups and 41 seeded-shuffled before/after pairs per size, with exact complete
result equality and branch/HEAD validation outside timing. Allocation peaks
are separate tracemalloc probes.

| Repositories | Guest calls before → after | p50 ms before → after | p95 ms before → after | p50 reduction |
| --- | --- | --- | --- | --- |
| 1 | 7 → 7 | 30.688 → 30.664 | 32.937 → 31.443 | 0.1% |
| 5 | 23 → 23 | 106.128 → 81.624 | 115.678 → 97.350 | 23.1% |
| 15 | 63 → 63 | 295.337 → 206.708 | 333.126 → 243.903 | 30.0% |

Peak awaited guest executions are one before and four after for multi-repo
cases. Python allocation peaks are 394,709 → 394,291 bytes for one repository,
466,894 → 562,428 for five, and 563,441 → 867,842 for fifteen. The additional
~0.29 MiB at fifteen repositories accompanies bounded concurrent tasks/SSH
buffers. These are Python allocation peaks, not process or guest RSS.

The improvement overlaps existing I/O and subprocess waits; it does not
reduce guest call count or promise lower total CPU work. Connection setup,
QEMU, remote network latency, Socket.IO and browser rendering are excluded.
No delays were simulated, and these local measurements are not production
latency guarantees. No UI code changed, so a screenshot would not establish
this execution-path improvement.

## Verification

- Before production edits: 229 runner tests and 14 subtests passed; no
  pre-existing test failure. The new parallelism regression test failed
  against the original sequential implementation, as expected.
- Targeted Git, containment, capture and real SSH checks: 298 passed and
  14 subtests passed.
- Full runner suite: 1,028 passed, 8 skipped and 36 subtests passed.
  Backend Git services/permissions, Socket.IO, MCP workspace adapter and
  architecture checks: 147 passed, with three existing Django deprecation
  and missing-static-directory warnings.
- New deterministic barrier tests cover discovery and HEAD concurrency,
  sorted results under reversed completion, deterministic first-error
  selection, failure drainage, timeout/cancellation recovery, shared budgets,
  unsafe metadata with a partially filled final batch, and capture admission.
- Ruff passes for new tests and benchmark changes. The service retains its
  seven pre-existing E501 diagnostics with no new diagnostics under the
  project's Python rules. `git diff --check` passes.

Additional candidates remain unfixed: runner file-read response parsing,
backend patch projection and mobile snapshot/block derivation.
