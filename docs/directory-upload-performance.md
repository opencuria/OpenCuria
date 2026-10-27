# Directory uploads: keep runner traffic responsive

Baseline: `0ab53c31c82dc459cd8ee099160827875c5652f9` on `origin/dev`.
Both AGENTS files were read in full, local/remote branches checked, and
`git fetch origin && git switch -C dev origin/dev` ran before code inspection.
The worktree was clean, so no stash was needed. Existing history, plugin,
Git, request-lifecycle and mobile streaming improvements were excluded.

## Investigation and selection

Backend, runner, webapp, mobile and shared packages were inspected. Candidates
were ranked by impact, frequency, measured confidence, complexity and risk:

| Rank | Candidate | Evidence and tradeoff |
| --- | --- | --- |
| 1 | Backend workspace-list relation reads | Current authenticated REST with 1/10/30 workspaces uses 9/27/67 queries and 10.5/23.1/51.5 ms p50. Credential/plugin reads repeat per row. Frequent navigation path; batching must preserve service/repository boundaries and visibility. |
| 2 | Backend realtime workspace access | Actual service checks use 3 queries without credentials, 4 with them; 3.24/4.43 ms p50. The latter fixture has 20 synthetic 16-KiB credential values. Terminal input, file and desktop access load unrelated credential/image relations. A minimal identity projection could avoid these reads without caching authorization. |
| 3 | Runner directory-upload conversion | Real conversion of 5,000 files takes 346 ms p50 and stalls an independent event-loop probe for 345 ms. Runtime I/O was stubbed only in this exploratory probe. A bounded thread can keep unrelated requests responsive; cancellation and lifecycle admission need explicit coverage. |
| 4 | Backend inactivity sweeps | Every heartbeat refreshes each workspace through `get_workspace_or_none`, including full relations and a busy-session read. Source evidence; safety depends on reading fresh activity before stopping, so broad batching is riskier. |
| 5 | Mobile streaming snapshot publication | `ChatController.buildSnapshot` clones/freezes sessions and rebuilds lineage/gate projections on each accepted event. Immutable part sharing is already optimized. Device/render profiling is needed before changing publication semantics. |
| 6 | Web streamed Markdown | Whole-document parsing, sanitization and DOM parsing recur on throttled changes. Reference links and media behavior require characterization; source evidence only. |
| 7 | Shared descendant traversal | `collectDescendantSessionIds` repeatedly scans sessions until stable and is used by web/mobile gate and refresh logic. Potentially expensive for large, unfavorably ordered trees; normal depth limits reduce expected benefit. |

A previously reported shared busy-transcript merge was also remeasured: current
100/1,000/3,000-part fixtures cost 0.21/0.63/1.58 ms p50, so it was excluded.

The first three formed the shortlist before an OS-random draw at
`2026-10-27T18:03:46.721658+00:00`. Python `secrets.randbelow(3)` returned **2**,
selecting directory uploads. There was one successful draw and no redraw.

## Change and boundaries

`runner/src/services/files.py::FileManager.upload_file` previously called
`convert_archive_to_tar` synchronously. While it parsed, decompressed and rebuilt
the archive, the runner could not process unrelated terminal, stream, heartbeat
or cancellation events.

The same conversion now runs through `asyncio.to_thread`. A single lock owned by
the existing FileManager permits only one conversion worker across its workspaces,
matching the previous serialized conversion behavior and avoiding an executor queue
of conversion jobs. Waiting requests can be cancelled before starting a worker.

An active conversion is shielded and drained before cancellation propagates,
including repeated cancellation or a conversion error. Its lock and existing capture
admission remain held until it exits. Cancelled requests never reach `put_archive`.
After the new await, the existing capture fence checks the workspace lifecycle epoch
again so an old upload cannot write into a stopped/recreated workspace.

Archive parsing, path/link rejection, formats, byte/permission preservation, base64
limits, runtime writes and Socket.IO results retain their existing implementations.
No dependency, cache, protocol, backend policy or agent logic is added. Ordinary
file uploads retain their current path. Cancellation of an active conversion waits
for that conversion to finish; Python threads cannot be forcibly cancelled.

An interleaved single-pass parsing variant was measured and removed because it
increased CPU/latency. The final patch keeps the original parser unchanged.

## Measurement

The committed `scripts/benchmark_directory_upload.py` runs real chunk-upload
Socket.IO handlers, WorkspaceService, FileManager and capture admission. Real local
`realpath`, `mkdir` and `tar` processes perform the upload/extraction under a temporary
repository directory. Every extracted file is checked, and complete converted tar
SHA-256 digests must match across versions and samples. Fixtures are removed on exit.

Python 3.12.3, disposable 200/5,000-file gzip archives, three warmups and **31 timed
samples per version/case**. Baseline/current order alternates against the same
fixture, runtime and destination. A five-ms periodic coroutine measures excess
wakeup delay; the reported distribution is the **worst delay per upload**, not the
distribution of individual ticks. p95 uses nearest rank. No tests ran concurrently.

| Files | Worst loop stall p50, before → after | Worst loop stall p95, before → after | Full upload p50, before → after | Full upload p95, before → after |
| ---: | ---: | ---: | ---: | ---: |
| 200 | 27.957 → 5.837 ms | 49.679 → 28.693 ms | 45.478 → 47.972 ms | 96.741 → 114.919 ms |
| 5,000 | 413.688 → 20.717 ms | 482.781 → 35.861 ms | 661.900 → 676.451 ms | 768.648 → 741.017 ms |

For 5,000 files, worst-loop-stall p50 improves **95.0%** and p95 **92.6%**.
The benefit is responsiveness of unrelated runner traffic, not upload throughput:
full-upload p50 increases **2.2%**, while p95 decreases **3.6%**. Small-upload
tail timings are noisy and show an increase; no upload-speed improvement is claimed.

Runner CPU p50 at 5,000 files changes 451.400 → 462.016 ms (+2.4%). Separate
`tracemalloc` observations are 24,355,656 → 24,355,855 bytes (+199 bytes); the 200-file
case changes 725,771 → 725,811 bytes. These are Python allocation peaks, not RSS.
The converted tar is 9,738,240 bytes in the large case; digests match exactly.

This is a local runner integration measurement, without QEMU/SSH, provider
traffic, a browser or a device. GIL contention still causes short stalls; a worker
thread is not a real-time scheduling guarantee. An earlier separate-process run
showed a larger upload-latency difference, prompting the alternating comparison.
Raw measurements and the random-selection record are in
[`benchmarks/directory-upload.json`](benchmarks/directory-upload.json).

Reproduce from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_directory_upload.py \
  --baseline-ref 0ab53c31c82dc459cd8ee099160827875c5652f9 --samples 31 --warmups 3
```

## Verification

- Before production edits: relevant existing runner tests **99 passed**; full
  runner suite **912 passed, 7 skipped, 37 subtests passed**. No baseline failures.
- Four new responsiveness/cancellation/lifecycle cases failed on the original
  implementation before the fix; the eight archive/validation cases already passed.
- Final full runner suite: **924 passed, 7 skipped, 37 subtests passed**.
  New coverage includes actual tar extraction, all four compression formats,
  Unicode/PAX paths, permissions, unsafe paths, failed conversion recovery,
  unrelated file requests during conversion, cancellation before/after worker
  start, repeated cancellation, worker errors, capture drain and lifecycle fencing.
- Backend file/upload transport tests: **25 passed**.
- Backend capture interaction and Socket.IO admission tests: **76 passed**.
- New tests, runtime helper and benchmark pass Ruff lint/format checks using
  `backend/pyproject.toml`. Production code has the same pre-existing E501 at the
  unrelated file-read size error; no new lint diagnostics. `git diff --check` passes.
- The production diff stays in FileManager's upload scheduling/admission. No UI,
  service layering, agent semantics, credential resolution or frontend logic changes.
