# Git snapshot metadata: reduce guest round trips

Baseline: `997f56e` on `origin/dev`, 2026-10-28. Both AGENTS files were read,
local changes (including untracked mobile files) were stashed, branches were
checked, and `dev` was reset after fetching. No unmerged branch changes the
selected Git service. Existing Git discovery, state-marker and untracked-diff
optimizations were retained.

## Investigation and selection

The survey covered backend, runner, webapp, mobile and shared packages. Ranking
weighed impact, frequency, confidence, complexity, regression risk and architecture.
Only the selected candidate was changed.

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend workspace presentation | Authenticated 30-workspace REST scout: 127 SQL statements, 124.5 ms p50. Credential/plugin mapping and image-version references still perform per-workspace reads. Broadly used by both clients, but moving presentation reads behind services requires care. |
| 2 | Runner Git snapshot metadata | Complete snapshot scout: 13 guest calls, 27.9 ms local p50. Four independent metadata reads use separate SSH command channels. Git UI polls every four seconds and mutations refresh the snapshot. Fixed batching is localized and needs no cache/concurrency. |
| 3 | Runner large-file response parsing | Source inspection found `splitlines()` plus rejoining the entire wrapped base64 body. The service probe, which finished after the draw, measured 29.7 ms p50 / 19.4 MB peak allocations at 5 MiB and 723.7 ms / 386.8 MB at 100 MiB, excluding transport. Lower frequency than Git polling; newline normalization needs characterization. |
| 4 | Shared streaming reducer lookup | `findPart` and stream-boundary handling scan historical parts. The expensive immutable cloning and quadratic busy-merge candidates from older reports are already fixed. Further indexing risks mutable/immutable ownership complexity. |
| 5 | Mobile snapshot publication | `buildSnapshot` still clones/freezes session and todo rows and rebuilds lineage on publication. Source evidence; device profiling is needed to establish the remaining cost after the recent immutable-part optimization. |
| 6 | Web streaming Markdown | Full revisions are parsed, sanitized and traversed, already coalesced at 50 ms. Incremental parsing has higher correctness/security risk and needs browser frame evidence. |
| 7 | Shared descendant traversal | Repeated full-session passes in `collectDescendantSessionIds` depend on ordering/depth. Typical bounded subagent trees reduce likely impact. |

An actual OS-random `secrets.choice` over the first three selected
**runner_git_snapshot_roundtrips**. There was one draw and no redraw. The
[selection record](benchmarks/git-snapshot-reads/selection.json) and exploratory
measurements are retained beside the final results. Scout timings included other
verification work and are not the final comparison.

## Change and boundaries

`runner/src/git_snapshot.py` builds one fixed shell command for status, refs,
configured remotes and the default remote. A small shell function streams each
command's output unchanged, then appends a nonce-framed exit status. It buffers
no command output and creates no temporary files. The parser requires exactly
four complete results, or one failed primary status result, with no trailing
data. Partial or ambiguous responses fail closed without retries.

The original credential-sourcing prefix is shared with individual Git commands.
The marker and ref format remain literal positional arguments. Commands bypass
shell functions, handle optional failures even with sourced `set -e`, and never
embed credential values. Status failure stops subsequent reads; optional reads
keep their individual exit codes and existing empty-field behavior. Missing-Git
classification uses the same validator as individual commands.

`GitService._git_repo_snapshot_no_log` consumes those results using the existing
parsers. Both explicit snapshots and post-mutation refreshes already call this
method, so neither keeps a second implementation. Repository containment,
per-repo locking, HEAD fallback, merge-state containment/probes, history, stash
handling and the bounded timeout/error/cancellation path remain in place. The
metadata batch uses the existing 30-second read bound; the public operation
deadline is unchanged. Cancellation retains the runtime's existing behavior:
stop awaiting the SSH command, without killing its remote process.

The runner remains a Git executor. Backend business/agent logic and all public
REST/Socket.IO/MCP schemas are unchanged. No cache, retained index, dependency,
background task or frontend logic was added. Normal complete snapshots use
**10 guest executions instead of 13** (23.1% fewer), independent of file count.

## Reproduction and measurement

From the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_git_snapshot.py \
  --baseline-ref 997f56e --samples 51 --warmups 5 --delay-ms 0 5 20
runner/.venv/bin/python scripts/benchmark_git_snapshot.py \
  --baseline-ref 997f56e --samples 51 --warmups 5 --delay-ms 0 5 20 \
  --untracked-files 500
```

The benchmark calls the complete public snapshot operation, including validation,
locking, Git reads, parsing, history and stash reads. Temporary repositories have
a real commit, staged/unstaged changes and 1 or 500 untracked files. Git and marker
commands execute as real local processes; the existing test runtime maps guest
paths and emulates credential/containment probes. Host credentials are not sourced.

The paired mode loads baseline `GitService` source from Git under a private
module name, using the same runtime and unchanged pure Git helpers. Before/after
run against the same fixture in one process, reversing order each pair, with five
warmup pairs and 51 measured pairs per case. No tests ran during the final timing
comparison. p95 uses nearest rank. JSON response hashing and allocation tracing
run outside the latency timer; allocation probes also run after garbage collection.

Python 3.12.3, Git 2.43.0, Linux; milliseconds:

| Untracked files | Added delay per guest call | p50 before → after | p95 before → after |
| ---: | ---: | ---: | ---: |
| 1 | None | 18.735 → 18.452 | 67.473 → 62.910 |
| 500 | None | 31.332 → 29.092 | 79.649 → 77.501 |
| 1 | Simulated 5 ms | 137.023 → 118.285 | 144.941 → 126.574 |
| 500 | Simulated 5 ms | 143.551 → 123.948 | 154.426 → 134.958 |
| 1 | Simulated 20 ms | 332.396 → 270.764 | 341.319 → 282.187 |
| 500 | Simulated 20 ms | 337.930 → 277.675 | 349.924 → 285.904 |

Local p50 improvements are modest (1.5% / 7.1%), with substantial shared-host
timing variation. At simulated 20 ms per guest call, p50 falls 18.5% / 17.8%.
The deterministic result is three fewer SSH command channels per refresh; added
delays model transport sensitivity and are **not measured production SSH latency**.
No QEMU VM was running, so live VM/network performance was not measured.

Undelayed peak Python allocations change from 290,571 to 289,170 bytes for the
small fixture and 482,739 to 492,044 bytes for 500 files (+9,305 bytes / 1.9%).
Framing briefly retains a combined response alongside its slices; it introduces
no persistent state. These are Python allocation peaks, not RSS or guest memory.
Every complete before/after response hash matched across all six cases.

Raw final samples, counts, peaks and hashes:
[small fixture](benchmarks/git-snapshot-reads/paired-small.json),
[500-file fixture](benchmarks/git-snapshot-reads/paired-large.json).

## Verification

Before production changes: 193 Git tests and 14 subtests passed. Candidate
investigation also passed 116 backend workspace/image/access tests.

After implementation:

- Full runner suite: **955 passed, 8 skipped, 37 subtests passed**.
- Backend Git services, REST, Socket.IO, account permissions and MCP workspace
  tests: **158 passed**.
- Final focused Git suite: **219 passed, 14 subtests passed**.
- New coverage includes exact binary-output parity, hostile filenames/markers,
  credential sourcing, optional failure isolation, primary error diagnostics,
  missing binaries, malformed/truncated frames, timeout propagation, cancellation
  and lock release. Exec-budget tests cover 0/50/500 untracked files. Existing
  mutation, unborn/detached/worktree, stash and containment tests pass.
- Full project-style Ruff checks pass for the new module, new tests and benchmark;
  import/undefined-name checks pass for the affected production files. The large
  existing Git test file has three pre-existing F841 unused-local findings outside
  the edited sections; they were left untouched. `git diff --check` passes.

No frontend code changed, so UI rendering/device checks were not applicable.
The user's mobile work remains preserved in its include-untracked stash.
