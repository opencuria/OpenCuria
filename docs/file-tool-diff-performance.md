# Keep backend file-tool diffs off the event loop

Baseline: `28bed787514e66a076f34c045943f908be8ad877` on `origin/dev`,
2026-10-30. Both AGENTS files were read, local/remote branches checked, and
`git fetch origin && git switch -C dev origin/dev` ran before code inspection.
The working tree was clean; no stash was needed. No unmerged branch commits
touched the selected tool implementation. Reference repositories were unchanged.

## Investigation and random selection

Backend, runner, webapp, mobile and shared packages were inspected. Existing
workspace-query batching, compact timelines, mobile structural sharing and
request-lifecycle work were excluded. Candidates were ranked by measured impact,
path importance/frequency, confidence, implementation risk and architecture:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend standard file-tool diffs | A complete 3 MB/100,000-line edit took 216 ms p50 and stalled a concurrent observer for up to 283 ms in the scout. `difflib` runs synchronously in async `WriteTool.execute` and `EditTool.execute`, affecting other sessions as well as the tool. Strong confidence; offloading needs concurrency and cancellation checks. |
| 2 | Backend duplicate media reads | `ReadTool.execute` reads up to 5 MiB, then `_execute_media` reads the same path again with a larger cap even when the first read was complete. A 1 MiB PDF caused two accessor reads transferring 2 MiB. Avoiding the second read would remove a whole runner round trip and duplicated transfer for complete small images/PDFs. Ingestion caps and truncated reads need coverage. |
| 3 | Runner file-response normalization | `FileManager.read_file` splits the whole wrapped base64 response into line objects and rejoins it. The parser scout measured 13.95/189.37 ms p50 and 18.48/184.36 MiB peak extra Python allocations for 5/50 MiB raw payloads. These are parser-only figures, not full runner latency. Preserving whitespace/error contracts adds some risk. |
| 4 | Web streamed Markdown parsing | `HarnessMarkdown.vue` reparses/sanitizes/rebuilds the whole document on published text updates. It already coalesces updates at 50 ms. Reference links and media make incremental parsing substantially riskier; browser profiling is still required. |
| 5 | Shared session descendant traversal | `collectDescendantSessionIds` repeatedly scans all sessions until ancestry resolves. Both clients use it, including mobile snapshot publication. Unfavorable ordering/depth can amplify work, but ordinary shallow trees reduce likely impact. Source evidence only. |
| 6 | Mobile transcript sorting | `ChatMessages` re-sorts its message array when an immutable streaming snapshot changes. Virtualization and row memoization already exist. Large-history device profiling is needed before changing ordering or publication rules. |

The first three formed the shortlist before one OS-random
`secrets.choice(candidates)` draw selected **backend file-tool diff blocking**.
There was no redraw. The other candidates were not changed.

## Implementation and boundaries

`backend/apps/harness/tools/file_diffs.py` owns the existing pure patch calculation
and one lazily started `ThreadPoolExecutor` worker. Both standard write/edit tools
await it. Calculation code, unified diff text, line counts, result metadata,
filesystem access, permission gates and per-path lock scopes retain their behavior.
The old synchronous helper was moved, with no compatibility wrapper or duplicate.

The worker receives only immutable path/text values. It cannot access an accessor,
ORM, tool context or event bus. A queued calculation can be cancelled; an active
calculation finishes without writing files or publishing events. Cancelling a tool
still releases its async path lock promptly. Python joins the single executor
worker at process shutdown. There is no per-call thread/process, cache, new
dependency or change to REST, Socket.IO, MCP, provider or runner contracts.

An initial default-pool `asyncio.to_thread` version was rejected during an
in-memory concurrent scout: four 3 MB edits took 924 → 1,619 ms p50 and peak
Python allocations grew 38.8 → 153.6 MiB. Competing Python diff workers increased
GIL contention and scratch memory. The final single worker preserves serialized
diff computation and leaves other backend/default-pool work independent of this
CPU queue. The final local-file measurements below use a different accessor;
compare versions within each experiment, not absolute values across experiments.

## Comparable measurements

The opt-in benchmark executes complete production `WriteTool`/`EditTool` calls
against identical disposable local files through an async accessor. It loads
the original module from Git without changing the checkout. Every invocation
checks complete result equality and final file bytes against baseline.

Python 3.12.3; a single changed value in 100, 10,000 and 100,000 unique source
lines (3,000, 300,000 and 3,000,000 bytes). Each case warms both implementations,
then alternates before/after order for **20 samples per version**. A concurrent
1 ms coroutine records its **worst scheduling gap per invocation**; p95 is
nearest rank. Python allocation peaks use separate tracemalloc runs. No tests
ran concurrently with the final measurements.

| Tool / file | Observer gap p50, ms before → after | Observer gap p95, ms before → after | Complete tool/batch p50, ms before → after | Peak Python MiB before → after |
| --- | --- | --- | --- | --- |
| Write / 3 KB | 1.719 → 1.873 | 1.793 → 1.971 | 0.608 → 0.763 | 0.042 → 0.045 |
| Write / 300 KB | 19.590 → 6.497 | 85.525 → 7.257 | 19.616 → 17.246 | 3.397 → 3.400 |
| Write / 3 MB | 215.683 → 80.959 | 236.369 → 86.641 | 218.910 → 224.989 | 35.960 → 35.967 |
| Edit / 3 KB | 1.684 → 1.800 | 2.022 → 2.075 | 1.208 → 1.468 | 0.046 → 0.048 |
| Edit / 300 KB | 21.292 → 6.886 | 99.846 → 7.358 | 22.677 → 22.302 | 3.683 → 3.686 |
| Edit / 3 MB | 215.206 → 80.182 | 239.828 → 88.603 | 220.774 → 231.480 | 38.821 → 38.824 |
| Four concurrent edits / 3 MB each | 634.530 → 91.649 | 884.747 → 101.112 | 929.008 → 939.174 | 73.171 → 64.590 |

Large-edit p95 observer delay improves **63.1%**; the 300 KB edit improves
**92.6%**. This improves concurrent responsiveness, not diff CPU throughput.
Single-call allocations are essentially unchanged. Small calls pay about
0.2 ms scheduling overhead; the 3 MB edit's median completion time increases
about 4.8%. Python's GIL and allocation/GC pauses still cause observable stalls.

For four simultaneous edits on distinct paths, p95 observer delay improves
**88.6%**. Median batch time changes by +1.1%; p95 batch time is
1,010.143 → 1,001.297 ms. Peak Python allocations fall 11.7% in this local-file
case. The single worker avoids multiplying diff scratch buffers while queued
tools retain their own immutable inputs. This is responsiveness isolation,
not parallel CPU execution.

These measurements isolate the backend tool path. They do not measure production
network latency, SSH, provider streaming or browser/device frames. tracemalloc
does not measure whole-process RSS or native thread stacks.

Reproduce from `backend/`:

```bash
.venv/bin/pytest -q -s apps/harness/tests/benchmark_file_tool_responsiveness.py
FILE_TOOL_CONCURRENCY=4 .venv/bin/pytest -q -s \
  apps/harness/tests/benchmark_file_tool_responsiveness.py -k 'EditTool and 100000'
```

## Verification

- Before production changes: 93 existing standard-tool, robustness and patch-event
  tests passed. Two new worker/event-loop assertions failed on the original code.
- Final targeted suite: 164 tests passed, covering file tools, same-path concurrent
  edits, permission boundaries/flows, patch events and runner tool ordering.
- New deterministic regressions cover a pending worker with an available loop,
  exact metadata, active and queued cancellation, lock release and worker errors.
- Six single-call and one four-call benchmark cases passed complete result/file
  equality checks.
- The moved pure calculation has an identical AST body to baseline, excluding
  its docstring.
- Ruff checks and formatting for new Python modules, Django system checks, and
  `git diff --check` passed.
- Two existing Django `CheckConstraint.check` deprecation warnings remain.
- Frontend and runner code were unchanged; no UI verification was required.
