# Runner file-read response parsing

The runner previously split every line of a base64 file response and joined the
body again before returning it. Large reads paid for hundreds of thousands of
temporary strings on the executor's event loop. Splitting only the two size/MIME
headers removes that work while retaining the guest's wrapped base64 body.

## Investigation and selection

Started from `origin/dev` at `9203109`. Both AGENTS files were read, local and
remote branches checked, an include-untracked stash attempted (the checkout was
clean), and `dev` fetched/reset before inspecting code. The relevant performance
branches were already merged; unmerged branches did not contain this change.

The survey covered backend persistence/services, runner execution/files/Git,
Vue chat rendering, native chat publication and both shared client packages.
Candidates were ranked by latency/resource impact, path frequency, evidence,
implementation risk and ownership:

| Rank | Candidate | Evidence / likely impact | Risk and scope |
| --- | --- | --- | --- |
| 1 | Backend pending-question index | Real repository against 60,000 retained questions: 11.771 ms p50; query plan scans the table and sorts. A temporary compound index reduced it to 0.454 ms. Timeline/feed reads pay this as history grows. | Low; additive migration, unchanged repository/service boundary. |
| 2 | Runner file-read response parsing | A roughly 20 MiB response-body probe took 63.226 ms p50 with 76.3 MB traced peak allocations. A bounded header split took 7.425 ms and 55.9 MB. Large file/media reads block all executor events during parsing. | Low; localized transport handling, no new state or scheduling. |
| 3 | Backend attention gate projection | With an experimental supporting index, 50 pending 4 KiB question bodies took 1.708 ms / 522 KB to load; an ID projection took 0.630 ms / 31 KB. Conversation attention only needs session IDs. | Low; repository projection and service caller, full detail still needed elsewhere. |
| 4 | Runner file autocomplete | The command traverses and sorts all matching files before keeping at most 51. Interactive path, but only source evidence was collected. | Preserving shallow-first ordering and pruning needs filesystem profiling. |
| 5 | Shared/mobile stream scans | Reducers scan messages/parts per delta; native message ordering scans/sorts each immutable snapshot. Existing structural sharing and bounded history already help. | Actual incremental benefit needs device/controller profiling. |
| 6 | Web Markdown streaming | Parsing, sanitization and DOM traversal process the full growing document. Already coalesced into 50 ms windows. | Incremental parsing risks reference-link, media and sanitization semantics. |

One actual `secrets.choice(top_three)` draw selected **file_read_response_parsing**.
There were no redraws. [Selection](benchmarks/file-read-parsing/selection.json)
and [scout results](benchmarks/file-read-parsing/scout.json) retain the evidence.
Only the selected runner bottleneck was changed.

## Implementation and behavior

`FileManager.read_file` now splits the LF-delimited guest response at most twice.
The MIME fallback, numeric size parsing, body trimming, missing-header error,
wrapped base64, source size and truncation flag retain their existing behavior
for the shell-generated response. An unterminated MIME line remains accepted;
a response with only a size line remains an error.

The optimization stays inside the executor's file service. It adds no dependency,
cache, worker, await point or retained state. Containment, quoting, size limits,
capture admission, four-channel read semaphore, error propagation and Socket.IO
handlers are unchanged. Backend business logic and frontend rules remain with
their owners. Full response buffering remains; this reduces processing overhead
after the guest read rather than introducing another transport.

## Measurement

The baseline was recorded before editing production code: 31 samples after
three warmups measured a 20 MiB complete local read at 121.162 ms p50 and
136.374 ms p95. Its response replay took 67.282 / 69.383 ms. The raw
[baseline](benchmarks/file-read-parsing/baseline.json) is retained.

The final comparison loads the original `FileManager` from Git without changing
the checkout and interleaves 41 before/after pairs in seeded shuffled order:

```bash
runner/.venv/bin/python scripts/benchmark_file_reads.py \
  --baseline-ref 9203109 --samples 41 \
  --output docs/benchmarks/file-read-parsing/paired.json
```

Both implementations use identical disposable binary files and real local
containment, shell, stat, file and GNU base64 commands. Response replay feeds an
actual captured guest response through the public service to isolate processing
cost. Every result is compared with the baseline payload, including MIME, exact
wrapped base64, source size and truncation; each complete read still makes two
guest calls. A separate 1 ms heartbeat observes scheduling gaps. Fixture creation,
validation and allocation tracing stay outside timing; p95 uses nearest rank.

| File size / simultaneous reads | Complete p50 before → after | Complete p95 before → after | Response replay p50 before → after |
| --- | --- | --- | --- |
| 4 KiB / 1 | 5.925 → 5.923 ms | 13.555 → 12.033 ms | 0.319 → 0.286 ms |
| 5 MiB / 1 | 43.329 → 27.683 ms | 51.313 → 35.081 ms | 16.905 → 1.831 ms |
| 20 MiB / 1 | 144.543 → 96.197 ms | 155.665 → 101.534 ms | 67.018 → 5.694 ms |
| 5 MiB / 4 | 118.793 → 58.923 ms | 126.787 → 68.738 ms | 70.205 → 7.358 ms |

For 20 MiB, complete-read latency falls **33.4% p50 / 34.8% p95**. Processing
p50 falls **91.5%** and its p95 maximum heartbeat gap falls **69.173 → 6.190 ms**.
Complete-read traced peak allocations fall **128.07 → 108.09 MiB (15.6%)**;
response-replay peaks fall **74.02 → 54.04 MiB (27.0%)**. Four concurrent 5 MiB
reads improve complete p50 **50.4%** and p95 **45.8%**; their complete-read p95
heartbeat gap falls **36.590 → 8.694 ms**.

[Raw paired samples and hashes](benchmarks/file-read-parsing/paired.json) include
all workloads. These are Python 3.12.3 local integration/CPU results, excluding
QEMU, SSH, network and provider latency. Traced peaks are additional Python
allocations, excluding fixture/child-process memory, not process RSS. Small-file
latency remains dominated by process scheduling; large response processing still
occupies the event loop for several milliseconds.

## Verification

- Before implementation: 75 existing runner tests and 11 subtests passed;
  57 backend conversation/query tests passed. No pre-existing test failures were
  observed in these selections.
- The added characterization cases passed against the original parser. The new
  allocation regression test intentionally failed there: 3,877,477 bytes against
  a 3,187,201-byte budget for a 1 MiB file. It passes with the bounded split.
- Coverage includes empty/text/binary files, shell-sensitive filenames, wrapped
  output, truncation boundaries, default MIME, missing headers, invalid sizes,
  read failures, invalid limits and independent concurrent responses. Existing
  tests cover path escapes, capture fences, cancellation and chunked RPCs.
- Final full runner suite: **1,172 passed, 8 skipped, 36 subtests passed**.
  Backend accessor/tool/capture/architecture selection: **163 passed**, with two
  existing Django deprecation warnings. These include file transport boundaries,
  containment, cancellation and capture behavior. No frontend code changed.
- New test/benchmark Ruff lint and format checks pass. Linting the touched
  production module also reports its existing 93-character size-limit message
  at line 456; the same error is present in the baseline. No unrelated cleanup
  was included. `git diff --check` passes.

Browser/device verification does not apply to this executor-only change.
