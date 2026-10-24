# Directory download performance

Measured on 2026-10-24 against `d951a66`, the latest `origin/dev` at the start.
The working tree was clean. Local/remote branches, including existing runner
and frontend performance branches, were checked before investigating. No
reference repositories or existing stashes were changed.

The final change is rebased onto `e384339` (QEMU-only runner). Its upstream
changes to `FileManager` concern initialization and terminology; the directory
download algorithm measured here is unchanged. Test fixtures use the current
QEMU-only interfaces.

## Investigation and random selection

Candidates were ranked by expected latency/resource savings, path frequency,
evidence, implementation risk and architectural cost. All five major areas
were inspected; only the selected runner path was changed.

| Rank | Candidate | Evidence and likely benefit | Risk / architectural cost |
| --- | --- | --- | --- |
| 1 | Backend plugin catalog: `PluginService.list_visible` / `_serialize` | Three nested-relation queries and an activation query per plugin: `1 + 4N`. A disposable Django fixture confirmed 209 queries for 52 visible plugins. REST and MCP use this service. | Low/medium: batch fetching must retain organization visibility, nested ordering and activation scope. |
| 2 | Backend workspace plugin readiness: `PluginService.list_workspace_plugins` / `_workspace_required_gaps` | Per-plugin requirement reads, plus OAuth server reads per OAuth requirement. A fixture with 50 enabled plugins confirmed 54 queries even without OAuth. Affects workspace plugin selection. | Medium: batching must preserve fail-closed credential ownership and OAuth checks. |
| 3 | Runner directory download: `FileManager.download_file` | Two complete tar/gzip passes: one to count bytes, then another to send them. Real local commands showed a size-only pass taking about 0.5 seconds for 20.8 MB. Full service baseline confirmed four runtime calls and about 960 ms median latency. | Medium: a single archive needs bounded temporary storage, reliable cleanup and unchanged download/chunking contracts. |
| 4 | Mobile streaming: `ChatController.prepareEventMessages` | Each event clones the active message and all existing parts, including nested input/meta/display fields. Work grows with tool-heavy turns. Existing immutable snapshots protect unrelated message identities. | Higher: selective copying must preserve reducer semantics and native snapshot immutability, and needs device/render verification. |
| 5 | Shared subtask matching: `buildChildSessionIdMap` | Each unmatched part filters the unmatched children again to resolve titles. Quadratic work for wide subagent groups; used by web presentation. | Medium: preserve ambiguous-title and chronological fallback behavior. Typical groups may be too small for large user-visible gains. |
| 6 | Web streaming Markdown: `HarnessMarkdown.vue` | Each rendered revision parses/sanitizes the complete document and builds a DOM. Existing 50 ms throttling already limits frequency. | Higher: reference links, sanitization, media lifecycle and actual browser responsiveness constrain incremental rendering. |

An actual `secrets.choice` draw over the first three candidates selected
**runner directory download duplicate compression**. No candidate was rejected
or substituted after the draw.

## Implementation and boundaries

`runner/src/services/directory_archive.py` builds a static guest shell command.
Paths remain separate positional arguments; `tar --` protects leading-dash
names. `FileManager` retains workspace lookup, containment and result handling.
No backend business logic, provider logic or frontend state moved into the
runner. No dependencies, caches, locks or persistent state were added.

The guest opens independent read/write descriptors for one private temporary
file and unlinks it before compression begins. Tar/gzip runs once, then the
same archive is encoded by the existing native `base64` utility. The size in
the result describes those exact bytes. Regular-file downloads are unchanged.

A POSIX shell file-size limit caps storage at the 100 MiB download limit plus
at most one 512-byte block. Lower inherited limits remain respected. Exact
compressed size is checked before any payload is emitted, so oversized
directories return the existing small `ValueError` response. Failed tar runs
discard partial archives and return `RuntimeError`. Guest exit closes the
unlinked descriptors, including after failure or interruption. QEMU caller
cancellation still stops waiting without killing the guest command; cleanup
requires no follow-up RPC. Each concurrent request owns its own descriptors.

This trades the duplicate compression pass for temporary guest storage of at
most 100 MiB + 512 bytes per in-flight directory download. It uses the existing
Linux guest shell/coreutils/tar/gzip environment and `/proc/self/fd`.

## Reproducible measurement

From the repository root:

```bash
runner/.venv/bin/python scripts/benchmark_directory_download.py --revision d951a66 --samples 21
runner/.venv/bin/python scripts/benchmark_directory_download.py --samples 21
runner/.venv/bin/python scripts/benchmark_directory_download.py --revision d951a66 --pairs 1 --samples 31
runner/.venv/bin/python scripts/benchmark_directory_download.py --pairs 1 --samples 31
```

The script loads the original `FileManager` directly from Git for the baseline,
without switching or modifying the checkout. Both versions execute real local
subprocesses through the same runtime test adapter, including containment,
directory detection, tar/gzip, base64, result parsing and returned size. The
larger deterministic fixture has 128 source files and 128 binary assets,
20,840,448 input bytes and an approximately 16.8 MB compressed archive. The
small fixture has two files and 162,816 input bytes.

Each case warms once before timing. Measurements ran sequentially with no test
suite running; p95 uses nearest rank. Every measured archive is decoded and
its ordered member names/content hashed outside the timed interval. These
hashes match before/after for both workloads. Metadata timestamps differ
between fixture creations, so compressed byte counts can differ slightly.
Child CPU and a separate tracemalloc run supplement wall-clock measurements.

| Metric | 20.8 MB baseline → optimized | 163 KB baseline → optimized |
| --- | --- | --- |
| Samples | 21 per version | 31 per version |
| p50 latency | 964.686 → 516.467 ms | 24.792 → 16.173 ms |
| p95 latency | 1,179.154 → 662.359 ms | 79.127 → 18.125 ms |
| Median child CPU | 985.922 → 502.005 ms | 25.996 → 15.288 ms |
| Runtime calls per download | 4 → 3 | 4 → 3 |
| Compression passes | 2 → 1 | 2 → 1 |
| Peak runner Python allocations | 68,230,515 → 68,224,017 bytes | 558,897 → 554,542 bytes |

The representative workload improves p50 **46.5%**, p95 **43.8%** and child CPU
**49.1%**. Runner allocation peaks are effectively unchanged. The smaller case
also improves; there is no extra interpreter startup in the guest command.
Shared-host scheduling makes p95 noisy, especially for small workloads. The
deterministic regression guard checks one compression pass and three calls.

Raw results are committed in [benchmarks/directory-download](benchmarks/directory-download/).
The benchmark measures local execution, not deployed QEMU or network latency.
No test VM was running; the existing stopped VM was left untouched.
Guest RSS, tmpfs memory and live SSH behavior
were not measured. The disk bound and cleanup are covered by real-command
tests rather than inferred from runner allocation measurements.

## Verification

Before production edits, 111 existing runner file/handler/security/capture
tests passed (plus 12 subtests). The backend plugin investigation suite also
passed all 59 tests.

After implementation:

- 128 focused runner tests pass, including 17 new real-command cases covering
  one compression pass, exact/over-limit archives, kernel-enforced spool size,
  metadata/symlinks, quoted/leading-dash names, tar failure, concurrent requests,
  cancellation cleanup, containment, invalid headers, and real Socket.IO
  chunk ordering/metadata and small oversize errors.
- 82 backend capture/authorization and download-chunk forwarding tests pass.
- Final rebased runner suite: **831 passed, 7 skipped**, plus 33 passing
  subtests. The original `d951a66` checkout had four pre-existing Codex
  configuration assertions expecting `"multi_agent" = false`; all four
  reproduced on an untouched baseline archive. The upstream `e384339` merge
  corrected those assertions. Optional QEMU dependencies account for skips.
- Ruff style/import/undefined-name checks pass on the new module, tests,
  runtime helper and benchmark. Import/undefined-name checks pass on
  `files.py`; its unrelated existing long line at line 353 still triggers E501
  in a full-file style check. `git diff --check` passes.

No frontend code or behavior changed; UI-specific checks are not applicable.
