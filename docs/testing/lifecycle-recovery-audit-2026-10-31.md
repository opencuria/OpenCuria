# Lifecycle recovery audit — 2026-10-31

This follow-up audits the current implementation, rather than inheriting the
acceptance claims from the [earlier audit](lifecycle-audit-report.md).

## Responsibility contract

| Concern | Backend owns | Runner owns |
| --- | --- | --- |
| Workspace lifecycle | Authorization, desired configuration, admission/capacity reservations, durable operation identity, workflow progression, authoritative API state | Execution of one bound command; libvirt/QEMU guest, disk, network and SSH state; local serialization and physical postconditions |
| Images | Recipe rendering and immutable revisions, version/line identity, current/pending assignment, capture replacement policy, retention, deletion approval | Disposable build guests, bounded execution/queue, verified QCOW2 publication and manifest, physical dependencies and exact safe deletion |
| Credentials | Selection, visibility, resolution, desired attachment, delivery-time secret resolution | Materialization, replacement, scrub, guest-incarnation checkpoints; absence of scrub proof must remain unknown |
| Native harness | Provider/agent loop, prompts, tools, questions/permissions, run/session/message state, restart recovery | Executes requested tool/process operations; has no agent policy |
| External harness | Attempt ownership, heartbeat, admission, settlement and transcript; periodic stale-run recovery | External process/lease identity, isolation, termination and confirmation of release |
| Recovery | Chooses whether evidence closes a workflow, allocates a new authorized attempt, releases backend reservations | Persists exact outcomes; fences duplicate/delayed delivery; observes whether effects have ended; replays unacknowledged results |

The backend must not infer physical absence from a timeout, delete local runner
files, or rerun unknown image scripts under a new identity. The runner must not
choose image versions, authorize destructive dependency changes, orchestrate
capture replacement, or decide whether a conversation may start another agent
turn. Similar state names on the two sides do not make them interchangeable.

Relevant boundaries are `services/domains/workspace_lifecycle.py`,
`services/capture.py`, `services/recreate.py`, `services/operation_results.py`,
`operations.py` and `services/disposition.py` in the backend, and `journal.py`,
`services/workspace_lifecycle.py`, `services/images.py` and
`runtime/qemu_runtime.py` in the runner.

## State and transition contract

These are separate state axes because they answer different questions.

| Axis | States and allowed progression |
| --- | --- |
| Backend task execution | `pending` → `in_progress` → `completed` / `failed`; a delivery timeout can provisionally fail the task while exact runtime evidence is still awaited |
| Durable command delivery/proof | `intent` → `delivery` → `executing` → `result`; ambiguous execution → legacy internal `intervention` → exact `result` or sealed `disposed`; a typed, proven recovery contradiction enters `attention_required` and remains subject to inspection |
| Workspace | New allocation → `creating` → `running` / `failed`; authenticated stop/start → `stopped` / `running`; approved deletion → `pending_deletion` / `deleting` → `deleted` / `delete_failed` |
| Image generation | `building` / `capturing` → `ready` / `failed`; retire or delete → `retired` / `pending_deletion` / `deleting` → `deleted` / `delete_failed` |
| Image assignment | Current usable version and pending build are separate pointers; only proven publication promotes a pending version; a failed successor preserves the previous usable version |
| Capture workflow | `stop` → `capture` → replacement workflow, or `resume` → `completed` / `failed`; initially stopped sources start at `capture`; any interrupted child must be settled before releasing its parent |
| Recreate workflow | `remove` → `create` → optional `stop` → `completed` / `failed`; removing old storage is the boundary at which the pinned target becomes the base |
| External harness attempt | `starting` → `running` → `closing` → terminal completion/failure/interruption; stale ownership requires runner lease release before settlement |
| Activity presentation | `active` with `in_progress` or `recovering`; closed outcomes in `history`; proven unrecoverable inconsistency or changed destructive approval in `attention` |

`intervention` is a compatibility storage label for automatic verification, not
an instruction to ask an administrator. `Workspace.intervention_required` is
likewise the existing API field for a retained failed-task fence. It remains a
blocking safety condition; presentation must explain automatic recovery rather
than offer an administrator acknowledgement as the normal path. Credential synchronization has its own blocking operation,
`synchronizing_credentials`, visible to the client as “Syncing credentials…”.

| Event / evidence | Automatic resolution |
| --- | --- |
| Preparation died before any delivery | Fail and dispose local intent; release its reservation without claiming an existing guest is absent |
| Offline runner or withheld, never-issued work | Retain durable queued command; deliver the same identity after reconnection/approval |
| Live queue/execution heartbeat | Renew execution deadline; do not mistake a queued guest or slow work for lost ownership |
| Terminal exact known result | Apply atomically; acknowledge replay; duplicates cannot alter a newer operation |
| Interrupted identity, sealed and complete quiescence proof | Close failed history, preserve storage, derive observed running/stopped state, release reservation automatically |
| Interrupted create/clone | Mark failed even if the VM is running: initialization was not proven; ordinary reset/delete remains available |
| Failed capture with known completed execution | Retain original workspace and previous images; resume when the capture workflow permits; release capture reservation |
| Interrupted capture with unknown outcome | Do not replay capture/init or blindly restart; settle after proof, retain resources and unlock normal actions |
| Ordinary publication error before manifest commit | Remove only this invocation's exclusively owned unpublished files; keep previous publications |
| Publication committed before runner crash or final durability error | Retain and verify exact manifest/digest; recover the committed result rather than rebuilding |
| Missing legacy credential lifecycle identity | Adopt an inspection-only command; require upgraded-runner sealing and proven guest quiescence; close conservatively without replaying secrets |
| Credential IO or repository clone hangs | Bound physical execution/waiting; retain unknown credential/effect state; settle through normal lifecycle recovery |
| Image recipe exceeds execution limit | Fail build, dispose its temporary VM, release slot; previous current generation remains usable |
| Native backend process dies | On exclusive backend startup, abort lost turns, error unfinished parts, reject dead input gates and release dead provider holders |
| External worker dies | Periodically inspect stale durable attempt; settle only after its runner process lease has ended; offline/accessor failure must not starve other attempts |
| Image deletion exhausts safe retries | Ordinary retryable failure in history; retain original approval for a repeated deletion request |
| Complete graph proves a dependency cycle, or completed removal contradicts its tombstone | `attention_required`; retain resources, explain administrator repair, automatically recheck after repair |
| Destructive dependency graph changes | Require renewed approval for the changed resources; the system cannot authorize those additional effects itself |

An offline physical host, unreadable backing chain, inaccessible filesystem or
contradictory ownership cannot be repaired by declaring resources absent. Those
conditions remain fenced with diagnostics while automatic verification retries.
Host/storage repair may be necessary. No timeout-based destructive escape hatch
is introduced, and this audit does not promise recovery from arbitrary hardware
failure or arbitrary database corruption.

## Defects corrected in this audit

1. **Delivery starvation.** The first 100 due commands could be the same offline,
   live, withheld or not-yet-prepared commands every tick. Other commands could
   never be delivered. The bounded batch now orders by due time and rotates
   every inspected entry, without consuming delivery attempts for waiting work.
2. **Publication leftovers.** Conversion, validation or fsync failure could leave
   truncated unpublished image files. Inventory then became incomplete and
   blocked unrelated deletion/recovery. Private per-operation staging now
   persists an ownership intent before creating disk outputs, records inode
   anchors, and supports exact cleanup after SIGKILL. Complete graph inspection
   protects external dependencies on any staging path or hardlink anchor.
   Rollback and restart recovery preserve a committed publication. Empty and
   metadata-only interrupted reservations cannot strand execution; unverified
   metadata is retained instead of guessed away.
3. **Unbounded image scripts.** Live heartbeats could preserve a hung build
   indefinitely. `RUNNER_QEMU_BUILD_SCRIPT_TIMEOUT` (default 7200 seconds) bounds
   upload/provisioning; existing guest cleanup and durable failure settlement
   release the build slot.
4. **Dormant harness recovery.** External stale-run sweeping existed but was not
   scheduled. ASGI now runs recovery periodically under the backend singleton;
   startup repairs lost native work and external orphan projections before
   scheduled dispatch can begin. Descendants of an unresolved external run remain
   protected until the ancestor's runner lease has been released. Recovery also
   publishes the settled state to connected chat clients. A separate process
   lifetime file lock prevents a live old ASGI process from overlapping startup
   repair even after its database lease expires. The descriptor stays held until
   OS process exit, including failed startup or shutdown while tasks drain.
5. **Misclassified administrator attention.** Automatic interruption recovery and
   ordinary failed deletion were shown as administrator work. They now appear
   as recovering activity or failed history. Explicit `attention_required` is
   reserved for a proven physical dependency cycle or a completed removal with
   a missing matching resource tombstone. Both are rechecked automatically;
   changed deletion approval still requires an explicit decision.
6. **Credential writes outside durable ownership.** Lost credential ACKs or a
   backend restart could leave a plain task open forever, blocking capture/reset.
   Injection now uses the same durable identity, sanitized outbox, journal,
   result validation and recovery as other lifecycle commands. A timed-out RPC
   does not discard the fence. Legacy tasks are adopted for inspection only;
   an upgraded runner must seal delayed plain delivery and prove that the exact
   guest has been quiesced. Missing credential proof stays conservatively dirty.
   Recovery never replays the old secrets. Duplicate unknown results no longer
   emit the same frontend error repeatedly.
7. **Unbounded workspace bootstrap IO.** Credential upload/replacement/scrub is
   bounded by `RUNNER_CREDENTIAL_SYNC_TIMEOUT` (120 seconds). Repository cloning
   is noninteractive and has a guest-side process deadline and kill grace,
   configured by `RUNNER_WORKSPACE_REPO_CLONE_TIMEOUT` (1800 seconds per repo).
   An SSH timeout never claims that a remote write finished; ordinary lifecycle
   recovery keeps the reservation until it can quiesce the guest.

The supported backend topology remains one ASGI process. SQLite places the
process lock beside the canonical data file. PostgreSQL source deployments can
set `BACKEND_PROCESS_LOCK_PATH`; server Compose uses a shared backend-runtime
volume. Every contender for the same backend must share that lock path/inode.
Distributed ASGI workers on unshared filesystems are not supported: native turns
would need durable write fencing for distributed takeover. A database lease
alone is not treated as proof that another native execution process died.

## Verification

The isolated live acceptance run passed **21/21 checks** using real ASGI, Redis,
runner, KVM/libvirt guests, QCOW2 and SSH. The cases cover host RAM pressure,
slow IO, abrupt guest shutdown, incomplete inventory, ENOSPC, build/runner
SIGKILL, publication-before-journal recovery, build deadline and subsequent
success, native startup recovery, stale external attempts, backend/runner loss
during capture, successful retry/replacement, persistent data, deletion,
credential attach/detach and actual external process termination before run
settlement. The latter uses a harmless managed `sleep` under an agent ownership
lease; no model provider was invoked.

All generated domains, networks, workspace disks and seed ISOs were removed.
The pre-existing user's stopped VM retained the same domain UUID and disk
identity/size. Sanitized reports are under
`/workspace/reports/lifecycle-recovery/`; the index is `README.md`, machine
summary is `summary.json`, preservation evidence is `cleanup.json`. Private
keys, databases and raw runtime state are not part of those reports.

The maintained full-stack reproducer is:

```bash
runner/.venv/bin/python scripts/verify_backend_runner_recovery.py \
  --cloud-image /path/to/ubuntu-24.04-server-cloudimg-amd64.img \
  --output /workspace/lifecycle-verification/fresh-backend-runner-run
```

It requires the installed backend virtualenv, local Redis/libvirt/KVM tools,
a fresh output directory and ancestors traversable by libvirt-qemu. The existing
`verify_workspace_faults.py` and `verify_workspace_crash_recovery.py` reproduce
the physical storage/admission and subprocess-crash groups. Publication crash
regressions also kill a separate subprocess while real throttled `qemu-img` is
converting and at the marker commit boundary.

Historical audit totals are not counted as checks of these changes. The live
acceptance and regression checks are complementary: later staging/ownership
hardening is tested by the named real-QCOW2 crash and process-lock regressions,
not silently attributed to an earlier VM run.

| Check | Result | Workspace artifact |
| --- | --- | --- |
| Complete backend runners suite, SQLite | 1622 passed; 21 row-lock cases skipped and exercised separately on PostgreSQL | `backend-tests.xml` |
| Real PostgreSQL 16 concurrency, disposition and credential recovery | 60 passed; no skips; isolated cluster stopped afterward | `postgres-tests.xml` |
| Complete runner suite including real image subprocess crash cuts | 1427 passed; 8 optional GUI/vendor-fixture skips; 36 subtests passed | `runner-tests.xml` |
| Harness, scheduler, deletion and activity regressions | 174 passed | `harness-tests.xml` |
| Process-lifetime ownership and ASGI startup contention | 10 passed | `process-ownership-tests.xml` |
| Web lifecycle/actions/store/activity/storage regressions | 130 passed | `frontend-tests.xml` |
| Lifecycle deployment and recovery worker checks | 4 passed; server Compose ownership volume parsed/validated | `deployment-tests.xml` |
| Web type check, changed-file lint and production build | Passed; existing bundle-size/deprecated-glob warnings | `typecheck.log`, `frontend-lint.log`, `build.log` |
| Final backend typed-attention, parent-workflow, durable/legacy credential and recovery delta | 72 passed | `backend-final-delta.xml` |
| Final runner typed diagnostics, real publication crash cuts, credential deadlines and journal recovery delta | 94 passed | `runner-final-delta.xml` |
| Python changed-line lint and whitespace | No added-line lint errors; existing unrelated lint debt retained | `lint.json`, `git diff --check` |
| Migration consistency | No drift; migration 0032 adds the explicit credential-sync operation choice | `migrations.log` |

The SQLite and full-runner suite totals are checkpoints before the final typed
permanent-recovery diagnostic addition. Its focused regression evidence is
reported separately, so overlapping checks are not presented as independent
coverage. Optional runner skips are tkinter (5) and unavailable pinned official
vendor/Node fixtures (3). Backend row-lock skips are not counted as passes.
