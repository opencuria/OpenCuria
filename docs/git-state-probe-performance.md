# Git operation-state probe batching

## Investigation and selection

Started from `origin/dev` at `38d44f8`, after reading both AGENTS files,
checking all local/remote branches, attempting an include-untracked stash
(worktree was clean), fetching, and resetting `dev` to `origin/dev`.
The existing request-lifecycle and web-resource performance branches were
already merged. The previous Git discovery optimization is preserved.

Investigation covered backend repositories/services, runner file and Git
operations, Vue chat rendering, native chat publication/list rendering, and
shared reducers/client events. Ranked by expected benefit, path frequency,
evidence, complexity, regression risk, and architectural fit:

| Rank | Opportunity | Evidence and expected benefit | Risk / scope |
| --- | --- | --- | --- |
| 1 | Backend plugin catalog N+1 | Query inspection: `PluginService.list_visible` does one plugin query plus four queries per plugin (skills, MCP servers, requirements, activation). Both REST and MCP use it. | Local relation/activation batching; must preserve org visibility and ordering. Not benchmarked in this run. |
| 2 | Runner Git operation-state probes | Four sequential guest marker probes on idle repositories; full snapshot baseline measured 16 guest calls. Selected details poll every 15 seconds; mutations also return snapshots. | Small executor-only change; retain metadata containment, marker semantics, locks and errors. |
| 3 | Runner Git repository summary reads | `_git_list_entry` runs two sequential HEAD/branch commands per repository; summaries poll every four seconds. | Consolidation could save one call per repo; detached/unborn behavior needs care. Adjacent to, but separate from, the already-merged discovery work. |
| 4 | Web streamed Markdown processing | `HarnessMarkdown.vue` reparses, sanitizes and constructs DOM for the growing document. | Needs browser profiling and syntax/media/streaming verification before choosing an incremental approach. |
| 5 | Mobile transcript publication | `ChatController` clones the changed message's parts, scans transcript byte estimates, and republishes session/transcript snapshots on deltas. Native list virtualization already exists. | Needs device CPU/allocation evidence; immutable snapshots must be preserved. |
| 6 | Shared streaming reducers | `routeAssistantMessage` and part lookup scan message/part arrays for each delta. | Needs realistic long-turn profiling; avoid another stateful index/cache without evidence. |

The top three were fixed before Python `secrets.randbelow(3)` selected index
**1** (one-based choice **2**): Git operation-state probes. No redraw occurred.
Only this opportunity was implemented.

## Implementation and boundaries

`runner/src/services/git_service.py::_git_merge_state` still resolves Git's
absolute metadata directory and calls the existing containment verifier on
every invocation. After verification, one fixed POSIX shell command checks
`MERGE_HEAD`, both rebase markers, and `CHERRY_PICK_HEAD`. Its builder lives
with existing pure command helpers in `runner/src/git.py`. The directory is
a separately quoted positional argument; no path is interpolated into script
source. Output contains at most three fixed flag names.

Ordinary snapshots remove **three guest execs / SSH channel round trips**:
16 → 13 overall, **18.75% fewer**. When `rebase-merge` exists, the old code
short-circuited one test, so that state removes two calls instead. The same
implementation serves explicit snapshots, post-mutation snapshots and merge
abort checks. There is no retained state, cache, new dependency, concurrency,
protocol change, or business/agent logic. Repository locks, auth sourcing,
timeouts, cancellation propagation and fail-closed metadata checks stay in
their existing layers.

## Measurements

Python 3.12.3, Git 2.43.0, Linux. Baseline is the archived service at the commit
above; optimized is this change. Raw samples and response digests are in
[`benchmarks/git-state-probes.json`](benchmarks/git-state-probes.json).

The strongest check executed the complete public `repo_snapshot` operation
using real Git, shell, `test` and `realpath` binaries inside a disposable
chroot. Each call used the production QEMU shell-command builder. The fixture
had 61 commits (50 returned), with staged, unstaged and untracked files.
There were five warmups and 51 samples per version, alternating baseline/current
order **per sample** on the same fixture. No injected delay, network,
credentials, mounts, SSH server or QEMU instance was used. p95 is nearest rank.

| Metric | Before | After | Improvement |
| --- | ---: | ---: | ---: |
| Guest calls / complete snapshot | 16 | 13 | 18.75% fewer |
| p50 | 55.674 ms | 43.493 ms | 21.88% |
| p95 | 179.268 ms | 165.220 ms | 7.84% |

All complete response hashes matched. Local scheduling produces noisy tails;
these are process-integration measurements, **not production QEMU/SSH latency**.
The eliminated calls are deterministic regardless of scheduling or repo size.

The portable benchmark uses the existing test runtime for workspace mapping
and credential/containment probes, real local Git and marker subprocesses,
and a small one-commit fixture with staged/unstaged/untracked content. It ran
two batches of 31 samples/version/case after five warmups, in before/after
then after/before order, without competing test work:

| Injected delay / call | p50 before → after | p95 before → after |
| --- | ---: | ---: |
| None | 22.502 → 18.893 ms | 29.138 → 25.245 ms |
| 5 ms, simulated | 113.599 → 138.111 ms | 170.746 → 144.635 ms |
| 20 ms, simulated | 403.874 → 334.389 ms | 414.043 → 341.018 ms |

The 5 ms case has an inconsistent median: baseline batch p50 varied from
111.488 to 161.022 ms. It does **not** establish a uniform latency improvement
at that delay. The paired real-process check above avoids separate-batch
ordering bias. The 20 ms case demonstrates the cost of eliminated round trips,
but its transport delay is simulated. Separate Python allocation probes were
284–286 KB before and 283–284 KB after; this does not measure guest RSS.

Portable reproduction, from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_git_snapshot.py --samples 31 --warmups 5
# Use the same script against an archived baseline checkout:
runner/.venv/bin/python scripts/benchmark_git_snapshot.py --repo /path/to/baseline
```

## Verification

- Before editing: 142 runner Git tests plus 14 subtests passed; 76 backend
  plugin/MCP/adapter-boundary tests passed during candidate investigation.
- After editing: full runner suite **862 passed, 7 skipped, 33 subtests passed**.
- New tests cover all 16 marker combinations, linked worktree isolation,
  literal shell arguments, failed/malformed probes, metadata containment,
  state changes, timeouts and cancellation. The complete snapshot test asserts
  the 13-call budget. Existing mutation, conflict, security, serialization,
  websocket and runtime tests pass.
- New tests and benchmark pass Ruff with `backend/pyproject.toml` conventions;
  existing modified files retain the same 22 pre-existing lint diagnostics,
  with no additions. `git diff --check` passes.
- No UI change; no browser/device test or real QEMU/SSH benchmark was performed.
  Final diff review found no service/repository boundary changes or unrelated edits.
