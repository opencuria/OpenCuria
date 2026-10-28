# Regular-file download performance

Measured on 2026-10-28 against `ea8ebfdda9ea87cc237ff18ea7eb1bc4b6c6c4e8`,
the fetched `origin/dev`. The worktree was initially clean; the requested stash
reported no changes. Existing performance branches were already merged.

## Investigation and random selection

Reviewed backend repositories/services, runner file/process execution, web chat
rendering/stores, mobile snapshot publication, and shared chat/client helpers.
Candidates were ranked by measured cost, path importance/frequency, confidence,
implementation risk, and architectural fit:

| Rank | Candidate | Evidence and assessment |
| --- | --- | --- |
| 1 | Runner large-file read parsing | `read_file` splits/rejoins every base64 line. Real local 5 MiB reads took 46.503 ms p50, with a separate parser measurement of 17.297 ms; at 25 MiB, 245.379 ms and 103.305 ms respectively. Affects media/file reads; localized but requires careful response-format coverage. |
| 2 | Backend message-edit history discovery | Edit validation and admission each load all message bodies. Two real ORM reads of 200 messages transfer 25 MiB: 21.811 ms p50 / 26.298 ms p95. User-visible admission work; session scoping and ordering must be preserved. |
| 3 | Runner regular-file download round trips | Four sequential guest executions for containment, directory probe, size, and content. A local 64 KiB download took 6.925 ms p50, excluding SSH. Strong latency opportunity in the actual SSH execution path; boundedness/security/error semantics need real-command coverage. |
| 4 | Mobile snapshot metadata cloning | Every publication clones/freezes sessions, todos, lineage and gates, including during streaming. Source evidence; realistic device profiling remains necessary. |
| 5 | Web streamed Markdown parsing | `HarnessMarkdown.vue` reparses and sanitizes the growing document, including DOM reconstruction. Source evidence; realistic browser profiling remains necessary. |
| 6 | Shared descendant-session traversal | `collectDescendantSessionIds` repeatedly scans the session list until ancestry resolves; reused in snapshot publication. Source evidence; ordinary shallow trees may be cheap. |
| 7 | Runner file mention search sorting | `find_files` sorts all matches before applying its small result limit. Source evidence; representative large-tree measurement remains necessary. |

The top three were fixed before an OS-random `secrets.choice()` draw selected
**runner regular-file download round trips**. No redraw was needed. The draw,
ranking and scout measurements are preserved in
[`benchmarks/regular-file-download/`](benchmarks/regular-file-download/).
Scout timings are diagnostic, not directly comparable to the paired results.
The disposable ORM scout was removed; no backend production code changed.

## Change and architecture

`runner/src/services/file_download.py` constructs one guest command which checks
whether the validated path is a directory, or checks a regular file's size and
executes base64. An oversized file exits before emitting content or starting
base64. Paths are positional arguments, never interpolated shell source.

`FileManager.download_file` keeps the existing containment check, consumes the
combined response, and shares size/status parsing with directory archives.
Regular downloads now use **two guest executions instead of four**; directory
downloads retain their existing three executions and archive implementation.
The final base64 process replaces its shell with `exec`.

This stays entirely within runner filesystem execution. No business, agent,
provider or permission logic moves layers. There are no caches, shared mutable
state, new dependencies, background resources, or frontend/backend contract
changes. Filename resolution, wrapped base64, byte counts, archive flags,
chunk ordering, capture admission and cancellation semantics are preserved.

## Comparable integration measurements

`scripts/benchmark_file_download.py` loads the original FileManager directly
from Git and compares both implementations on identical disposable files under
/workspace. Each case has three warmups and 41 interleaved, seeded-shuffled
before/after pairs. Every pair must have identical complete result dictionaries;
base64 is also decoded and checked against the input bytes outside timing.

The benchmark was first run before production edits, comparing original/current
code to validate the harness and establish local and SSH baselines. Both then
used four executions. Final results below come from the final paired runs.
Python allocation peaks are separate tracemalloc calls, not timing samples.

The SSH variant uses a persistent, host-key-verified AsyncSSH connection to an
isolated loopback OpenSSH server and the production
`QemuRuntime.exec_command_wait` implementation. Only connection acquisition is
injected. Connection setup/authentication is excluded, matching warm runner
connections. This exercises actual SSH channels, shell/stat/base64 execution
and result parsing; it does **not** include a QEMU guest, remote-network latency,
Socket.IO transfer or browser rendering. No artificial network delay is added.

| SSH workload | Guest calls before → after | p50 ms before → after | p95 ms before → after |
| --- | --- | --- | --- |
| Empty file | 4 → 2 | 23.364 → 13.611 | 28.746 → 19.542 |
| 64 KiB | 4 → 2 | 26.069 → 15.791 | 32.551 → 19.987 |
| 5 MiB | 4 → 2 | 61.655 → 53.391 | 70.803 → 59.381 |
| Four concurrent 64 KiB downloads, whole batch | 16 → 8 | 48.404 → 28.139 | 53.099 → 32.316 |

The 64 KiB case improves p50 **39.4%** and p95 **38.6%**. The four-download
batch improves p50 **41.9%**. Earlier paired SSH runs also improved every
workload's median; absolute times vary with shared-host scheduling.

Without SSH, final local subprocess p50 was 6.027 → 4.446 ms at 64 KiB and
24.477 → 16.291 ms for eight concurrent downloads. At 5 MiB it was
19.942 → 20.779 ms (p95 26.697 → 28.413 ms): the combined response requires
an additional large-string slice, so no large-file local speedup is claimed.
The production-style SSH 5 MiB path still improves 13.4% in median latency.
Replacing the final shell with `exec` reduced the local overhead observed in
intermediate runs. The targeted gain is eliminating SSH channels/round trips.

Final SSH peak Python allocations at 5 MiB were 14,210,238 → 14,197,298 bytes;
at 64 KiB, 362,645 → 316,756 bytes. Local 5 MiB peaks were
21,274,933 → 21,266,371 bytes. These single-call measurements show no material
allocation increase in these cases; they do not establish process or guest RSS.

The initial eight-download SSH scout exceeded OpenSSH's default ten-channel
limit due to channel-close overlap. The original implementation exhibited this
existing constraint before changes. Paired SSH concurrency was therefore fixed
at four on both sides; local subprocess concurrency remains eight. No channel
limit or connection/lifecycle policy was changed to obtain the reported results.

Reproduce the local comparison from the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_file_download.py
```

For a disposable loopback SSH server, add `--ssh-port PORT --ssh-key KEY_PATH
--known-hosts HOSTS_PATH`. Keys and server config are temporary environment setup,
not repository assets. The benchmark's CLI validates these required arguments.
Machine-readable pre-edit and final results are in the benchmark directory.

## Verification

- Before production edits: 64 runner file/path/chunk/archive/find tests and 71
  handler/capture tests plus 12 subtests passed. The exploratory backend edit
  suite also passed 65 tests; it emitted pre-existing pending-task and deprecation
  warnings. No pre-existing test failure was found.
- New execution-count regressions failed on the original implementation: nine
  file cases used four calls instead of two; the eight-download batch used 32
  instead of 16.
- Final relevant runner checks: 195 passed, plus 12 subtests, covering file reads,
  archives, chunks, capture fences, SSH connection ownership and exit status.
- Backend file-transfer ownership and capture/socket authorization contracts:
  63 passed (existing Django deprecation warnings only).
- New coverage exercises empty/text/binary content, special filenames, exact and
  exceeded size caps, missing files/FIFOs, symlink containment, malformed headers,
  stat/base64 failures, independent concurrent results, caller cancellation and
  ordered RPC chunks/small error responses. Existing directory tests still pass.
- New files pass Ruff lint/format; `files.py` passes lint excluding its existing
  unrelated E501 at the read-limit message (also reproduced from the baseline).
  Existing unrelated formatting in that file was left alone. `git diff --check`
  passes. No UI code changed, so frontend checks/browser verification do not apply.

The temporary loopback SSH server and credentials were removed after verification.
