# Image lifecycle architecture and operations

Current implementation reference (2026-10-03). This replaces the chronological
phase handoffs; historical “next phase” statements are not operational guidance.

## Architecture

Recipes (`ImageDefinition`) are editable catalog identities. Immutable
`ImageRevision` records exact rendered inputs; `ImageBuildJob` is the runner
assignment, with current/pending generation pointers. Every build allocates a
new `ImageInstance` UUID and unique runtime target. Failed rebuilds preserve the
previous current image; workspace pins never follow pointer changes. Exact
runner/task/assignment/generation correlations gate callbacks. Cancellation can
restore a completed still-latest build, but never revive superseded or independently
retired resources. Runner → assignment/resource lock ordering coordinates
allocation, selection, retirement and completion.

Backend services own business orchestration; repositories own ORM/locking.
`operations.py` and `locking.py` guard lifecycle allocation/results;
`services/recovery.py` is the independent durable worker. The runner is execution
only: `journal.py` records identity, outcomes, checkpoints and publication proof;
`runtime/storage.py` serializes scans/mutations. Storage, capture, deletion and
operator disposition repositories/services are separate concerns. Frontend and
MCP call those same services, not independent state machines.

## Preserving migration

Migrations 0017–0023 preserve definition/job/image/workspace IDs, refs and pins,
add generations, durable commands, inventory, capture and deletion intent tables.
Preexisting images are legacy with no invented historical revision. Unusable task
strings survive as legacy metadata, not fabricated Task rows. Nonpositive legacy
sizes become unknown. PostgreSQL migration drops old varchar-pattern indexes
before UUID casts and drains deferred trigger events before DDL; final FK/btree
coverage remains. Migration never dispatches physical changes. Back up the DB and
preserve runner bytes/journal before deploying both components together.

## Safety invariants

- Unknown size is null, never zero. Complete enumeration is not cleanup authority.
  Partial/offline/stale scans cannot prove absence or release destructive work.
- Physical dependency edges include foreign consumers and recursive QCOW2 backing
  chains. Unknown/unmanaged mounts and foreign aliases are read-only blockers.
- Force requires exact approved structural fingerprint, not a blanket bypass.
  Reconfirmation is required when dependencies change; foreign/shared resources
  cannot be forcibly adopted. Deferred intent waits without pretending execution.
- QEMU capture automatically stops and restarts running guests, and requires
  durable scrub evidence bound to unchanged domain/disk incarnation. Capture is
  standalone QCOW2, not an overlay. Stopped observation alone is not scrub proof.
  Only injected credentials are scrubbed; arbitrary user secrets are not sanitized.
  Failed capture may resume only its previously running guest; unknown outcomes
  retain fences. A successful capture survives a failed restart. Docker capture
  remains unsupported (legacy captures remain readable).
- Ordinary QEMU removal checks the reverse graph before destroying its domain
  and before unlinking its disk. Docker canonical workspace data volumes are
  explicitly labelled at creation. Removal checks all stopped/running consumers
  before container destruction, then confirms volume/network absence. A stable
  workspace UUID supports retry after container loss. Unlabelled legacy volumes
  require exact canonical container identity + `/workspace` mount proof; absent
  legacy containers require operator review. Bind/shared/foreign volumes are
  never unlinked. Network/volume failures propagate, never false success.
- External guest/container stop is observation only: never auto-start it.
  Interrupted create/build/capture is never blindly executed again. Retained
  fences require exact journal/runtime evidence and explicit operator action.

## Durable command wire contract

Every lifecycle command carries existing payload plus `operation_id == task_id`,
`attempt`, `target`, `runner_id`. Workspace target is workspace UUID, build target
is image instance UUID; existing image callbacks still independently validate
exact runner ref/task/image correlation. `operation:result` carries
`{event: <existing terminal event>, data: <existing result plus envelope>}`.
`operation:ack` contains operation_id, attempt, target, runner_id and is sent only
**after** the backend callback transaction commits terminal Task.status.
`operation:heartbeat` carries the same identity during running handlers.
Duplicates terminally ACK without applying again. Direct old terminal callbacks
cannot bypass durable envelope validation on the configured ASGI server.

Backend `apply_result` validates runner/type/task/target/attempt/current-task,
locks task/workspace and invokes existing callbacks in one DB transaction. Latest
authenticated SID supersedes prior SID; callbacks/heartbeat/register from an old
SID are rejected. Old disconnect cannot offline a newer SID. Controlled stop
clears `credentials_present` only on explicit false scrub evidence in its result;
a stopped observation, interrupted stop or lost reply is not scrub proof.

## Deployment

Run migrations, ASGI and `python manage.py recover_lifecycle` with the same DB,
credential encryption key, application version and REDIS_URL. Redis channel
`opencuria-runner` delivers messages, not durable truth (DB rows are truth).
Systemd template: `backend/systemd/opencuria-recovery.service`; compose services
include recovery and Redis. Runner journal is private FULL-sync SQLite/WAL under
RUNNER_STATE_DIR (default ~/.local/share/opencuria/runner); runner compose/systemd
persist it. Never delete that volume or run two processes against one journal.

Worker claims use leases/backoff; execution heartbeats suppress redundant delivery
but not hard deadlines. Offline runner observation does not rewrite runtime state.
Timeout/intervention retains current-task fences. Operation results ACK only after
backend terminal transaction commit; unacknowledged outcomes replay on reconnect.
The latest authenticated Socket.IO SID supersedes old callbacks/disconnects.

## Inventory contract

`RuntimeBackend.inventory() -> RuntimeInventory`; `WorkspaceService.inventory_for_runner()
-> dict` aggregates dataclasses using `asdict`. `runner:inventory` payload:
`{schema_version: 1, inventory_epoch: UUID string, inventory_sequence: integer,
complete: boolean, runtimes: RuntimeInventory[]}`. Each runtime snapshot has
`runtime_type`, UTC ISO `collected_at`, `complete`, `errors: string[]`,
`foreign_resource_count`, `filesystems`, `resources`. Filesystems expose path,
capacity_bytes, used_bytes, available_bytes, independently of image accounting.
Resources (`StorageResource`) have resource_id, kind, managed, aliases,
dependencies (physical resource IDs), state, allocated_bytes, logical_bytes,
virtual_bytes, shared_bytes, reclaimable_bytes, provenance and metadata.
All five sizes default null; Docker logical image bytes overlap layers and must
not be summed. Docker IDs are physical sha256 IDs, tag aliases are references;
QEMU IDs are absolute physical paths, backing edges include captures and recursive
external chains. Foreign domain/container references are read-only blockers;
foreign unrelated Docker assets/cache are counted only. Build-cache ownership
cannot be reliably proven from Docker's df API and is not asserted managed.
Docker volume bytes may be unknown and unlabelled mounted volumes are read-only.
A complete inventory means enumeration/inspection succeeded, not that every
resource is ready or cleanup-authorized. Manifest-less files remain unknown.

`inventory:refresh` takes an optional ignored payload and emits a new full snapshot.
Registration is followed by inventory. `RUNNER_INVENTORY_INTERVAL` defaults 120
seconds; full snapshots are separate from heartbeats. The scan loop continues
through disconnect and keeps its newest local snapshot; errors are represented
per runtime even offline. Backend must atomically validate epoch/sequence and
persist full snapshots, keeping partial scans from establishing absence.

### Publication and execution boundaries

QEMU publication converts into a unique same-directory temp, verifies standalone
QCOW2 and integrity, fsyncs, publishes exclusively, and writes an exclusive fsynced
manifest with digest/identity. Interrupted files remain unknown, not auto-removed.
Publication/checkpoint evidence can recover exact terminal success; otherwise
intervention preserves resources. Legacy metadata is read without rewriting.
Cold cloud-image downloads validate/fsync before publishing the cache entry.

External writers do not obey the process-local storage lock; this is not a
host-wide lease system. Exclusive publication and daemon consumer checks fail
closed where detected. Full digest scans favor correctness over performance;
optimized immutable-file caching needs independent validation.

## Storage and capture API



- Migrations 0019–0021 add InventorySnapshot, InventoryRuntime,
  InventoryResource, InventoryAlias, InventoryEdge, InventoryRefresh and
  CaptureRequest. Resources, aliases and dependency edges have normalized FKs
  and uniqueness constraints. Missing physical endpoints become unknown
  resources, not dropped graph edges. These migrations add tables without
  rewriting generation/workspace/assignment IDs.
- Authenticated `runner:inventory` is persisted transactionally. A previous SID,
  duplicate/reversed sequence, epoch switch in the same connection, malformed
  timestamp or negative size is refused. New connection SID allows a new epoch.
  Last complete per-runtime observation survives partial diagnostics. Freshness
  requires online runner, matching SID, received and collected timestamps within
  five minutes; future collection timestamps are not fresh. No observation
  automatically marks a workspace/image absent or backfills an unproven pin.
- `GET /api/v1/runners/{runner_id}/storage/` is cached inspection only (org admin,
  runners:read API-key scope). Returns runner_id, latest_snapshot_id,
  latest_complete, runtimes, generations, operations and capture_requests.
  Runtime entries expose fresh, snapshot_id, epoch, sequence, collected_at,
  received_at, filesystems, resources and latest diagnostics. Resource entries
  expose physical_id, kind, managed, state, five nullable byte measurements,
  aliases, dependencies, image_id, provenance and workspace identity/owner/status/
  last_activity_at. Generations include assignment/current pointer and revision.
  Docker logical/shared sizes overlap and must not be summed.
- `POST /api/v1/runners/{runner_id}/storage/refresh/` returns 202 with requested,
  runner_id, requested_at. Coalesced durable request accepts offline runners;
  independent worker sends inventory:refresh with backoff on the current SID.
  Only a subsequently received complete aggregate scan fulfills it. MCP tools
  `get_runner_storage` and `refresh_runner_storage` share StorageService and
  authorization. Unknown inventory sizes remain null; fresh managed matching
  physical observations update generation sizes.
- Minor runner contract correction: Docker container and managed QEMU domain
  resources now include metadata.workspace_id, allowing actual workspace joins.

### Capture pipeline

Artifact-create REST/MCP automatically stops and restarts running QEMU sources;
stopped sources remain stopped. No restart flag is accepted. Request, deterministic
image ID, first child Task/current_task and sanitized outbox are committed atomically.
Independent worker advances stop → capture → resume after each terminal callback;
no request-thread or GET-driven progression. Resume credentials use the existing
CredentialSvc resolution. Only injected credentials are scrubbed, not arbitrary
user secrets. Known safe capture failure may resume an explicitly prior-running
source; failed restart retains the successful capture. While capture owns the
workspace, user stop/remove and all other live interactions are rejected and cannot
suppress automatic restart. Active agents and pending credential synchronization
must finish before capture is admitted. Docker capture remains rejected.

API migration: callers must stop sending `stop_and_restart`; it is removed from
REST/MCP schemas. Capturing a running source now restarts it automatically, without
opt-in. The webapp displays one Capturing operation through stop/capture/resume;
only navigation and saved history remain available until completion.

Unknown/intervention child outcomes retain the workspace fence and diagnostic;
no blind resume is attempted. A stopped source with credential presence returns a
controlled resume/stop diagnostic rather than inventing scrub proof. Runner cold
cache still independently refuses capture without scrub evidence. Capture request phase/diagnostic is exposed in runner storage inspection.

## Deletion API / MCP

Base URL: `/api/v1/image-artifacts/deletions/`. All new routes require
`images:delete` API-key permission and organization membership. Preview and force
require organization admin. Members may defer/cancel their own workspace
captures only; admins may operate another owner's images in the same org.
Cross-organization IDs return not-found.

- `POST preview/`: body `{target_type: "image" | "assignment" | "definition",
  target_id: UUID}`. Read-only, returns HTTP 200 Graph (below).
- `POST /`: same target plus `mode: "deferred" | "force"` (default deferred),
  `fingerprint: string` (required matching fresh unblocked preview for force).
  Returns HTTP 200 Request. Repeated live-target requests reuse the intent.
  Force reconfirmation updates the existing held approval, never silently widens.
- `GET /`: HTTP 200 `Request[]`, authorized visible history, newest first.
- `GET {request_id}/`: HTTP 200 one-element `Request[]`; not-found if unavailable.
- `POST {request_id}/cancel/`: HTTP 200 Request; HTTP 409 after physical child
  release. Retirement restores previous state only if still pending_deletion;
  deleted state is never resurrected.
- Errors: HTTP 403/404/409 `{detail: string, code: string}`.

MCP equivalents: `preview_image_deletion`, `request_image_deletion`,
`cancel_image_deletion`, `list_image_deletions` (optional request_id also gives
status), with identical arguments/results/authorization. Existing assignment and
recipe MCP delete tools also use the coordinator. The old assignment tool now
returns the durable Request, not a misleading `deleted: true` result.

`Request` fields:
```
{
  id: UUID, target_type: string, target_id: UUID,
  mode: "deferred" | "force",
  phase: "waiting_inventory" | "waiting_offline" | "waiting_dependency" |
         "executing" | "reconfirmation_required" | "intervention" |
         "completed" | "cancelled",
  diagnostic: string, fingerprint: string,
  can_cancel: boolean,
  released_at: ISO datetime | null,
  created_at: ISO datetime, updated_at: ISO datetime,
  children: { "image:<UUID>" | "workspace:<UUID>": task_UUID },
  approval: Graph
}
```
`Graph` fields:
```
{
  roots: UUID[],
  images: {id: UUID, runner_id: UUID, owner_id: UUID | null, name: string}[],
  workspaces: {id: UUID, runner_id: UUID, owner_id: UUID | null, name: string}[],
  resources: {key: "runner_UUID:runtime:physical_ID", image_id: UUID | null,
              workspace_id: UUID | null, kind: string, managed: boolean,
              aliases: string[]}[],
  edges: [consumer_resource_key, dependency_resource_key][],
  pins: [workspace_UUID, image_UUID][],
  blockers: string[],
  snapshots: {runner_id: UUID, snapshot_id: integer}[],
  counts: {images: integer, workspaces: integer},
  fingerprint: SHA256_hex
}
```
Internal approval may additionally contain `retry_counts` (per member) and
`failed_children` (task UUIDs), retaining historical known-finished retry attempts.
Frontend should treat these as read-only diagnostic extensions. Waiting phases
are idle pending intent, **not an indefinite busy spinner**. Show diagnostic and
cancel where allowed. `executing` refers to prepared/active child execution;
`reconfirmation_required` must prompt for a new preview and explicit approval;
`intervention` must not offer blind retry/undo. Existing image/generation status
reflects retirement independently of availability pointers; use Request as
single deletion progress truth, not duplicate stale assignment error labels.

## Operator API / MCP

- `GET /api/v1/runners/operations/` lists sanitized visible operations.
- `GET /api/v1/runners/operations/{operation_id}/` requests current-session
  `operation:inspect`: identity, process instance, running/terminal/unknown,
  safe result, `outcome_known`, `execution_finished`, fresh inventory and
  `quiescent`. Offline inspection is non-destructive `unknown`; actions disabled.
- `POST /api/v1/runners/operations/{operation_id}/reconcile/` applies exact late
  proven terminal evidence; does not execute runtime handlers again.
- `POST .../retry/` allows only known finished failed start/stop/remove with
  a complete fresh scan and no live handler/helper process; new task identity,
  at most two explicit retries. No create/build retry.
- `POST .../acknowledge_interrupted/` requires organization admin, exact exclusive
  journal-process evidence and complete fresh runtime scan with no executing
  handler/helper. Marks failed completion and releases the fence **without
  deleting or recreating resources**. Never-delivered identities are first sealed
  as permanent journal tombstones so delayed packets cannot execute afterwards. Partial QEMU creation remains failed,
  disk intact; subsequent removal is a separate explicit action. Stop without
  an exact scrub checkpoint stays unknown, never invents clean credentials.

MCP parity: `list_lifecycle_operations`, `inspect_lifecycle_operation`,
`dispose_lifecycle_operation` (`action`: `reconcile`, `retry`,
`acknowledge_interrupted`). List/inspect require `runners:read`; disposition
requires `workspaces:update` plus the operation-specific lifecycle scope for
retry. Workspace owners inspect/reconcile their own; org admins inspect all;
interruption acknowledgment always admin. No resolved credentials/recipes
appear in these return values. List returns `{operations: [...summary]}`;
inspect returns summary plus `{evidence: {...}}`; disposition returns
`{operation_id, reconciled}` / `{operation_id, previous_operation_id}` /
`{operation_id, released, resources_preserved}`.

## Docker publication and accounting

Docker build labels are inherited by intermediate images: they identify managed
build cache, **not** publication. Only the exact immutable
`opencuria/generations:<generation UUID>` alias with generation/operation labels
identifies a final image. After successful build and tag/physical-ID verification,
the runner FULL-sync SQLite publication checkpoint retains the final physical ID.
Existing `image:built` journal records also provide this binding. Removing the
tag manually therefore does not make a published physical image disappear;
foreign aliases remain deletion blockers. A contradictory final tag/physical
checkpoint makes the scan incomplete rather than replacing identity. Backend
joins require an exact target alias or physical publication binding, never labels
alone, and reject multiple physical resources for a single generation.
Interrupted builds are never proved by intermediate labels. Known intermediates
remain visible as managed `build_cache` with overlapping logical bytes and no
image FK; enumeration can be complete with cache present. No automatic cache or
unknown-resource deletion is performed.

Docker inspect logical Size and system-df SharedSize can use different engine
accounting units. Raw df sizes remain source-labelled metadata; shared_bytes is
null when it would exceed inspect logical bytes, rather than inventing a clamp.
DockerRootDir filesystem capacity/available/used uses local statvfs only for a
local Unix daemon and accessible directory; remote/inaccessible values are null.

## Verification and limitations

Automated suites cover migrations, exact identities, stale/duplicate callbacks,
lease claims, capture checkpoints, graph blockers, retry/disposition, deletion
cancellation, and UI contracts. Prior verified PostgreSQL tests use independent
connections and observed lock waits; SQLite is not evidence of row-lock behavior.
The PostgreSQL async-focused suite can leave sessions preventing test DB teardown;
standalone concurrency runs close cleanly. Existing staticfiles/deprecation and
frontend chunk-size warnings are separate from correctness assertions.

Parent-completed live verification (isolated integration environment):

- PostgreSQL preserving migration/repository/recovery suites: **80 passed**,
  including four independent-connection concurrency tests. Subsequent
  cancellation/completion verification: **55 passed**. SQLite does not substitute
  for these observed PostgreSQL lock-contention tests.
- Real tiny Docker HTTP build/rebuild preserved old workspace pins. Deferred
  deletion/cancellation and approved force completed; all fixture containers and
  three image generations were physically absent, with every child journal ACK
  confirmed.
- Backend outage during the real 12-second Docker build preserved the exact task's
  terminal `image:built` with ACK=0 before restart, then ACK=1 after reconnect,
  with exactly one runtime generation. This was lost-outcome replay, not rerunning
  a new task; no running container was stopped for the outage test.
- Canonical Docker volume smoke passed: actual data volume, shared-consumer
  preflight refusal, inventory size/identity and complete cleanup.
- Real libvirt stopped blank-disk standalone capture and dependency checks passed.
  This does **not** establish boot, SSH, guest credential scrub or provisioning.

Commands and private evidence filenames are documented in
`e2e/integration/README.md`; `outage-before.json` retains pre-reconnect ACK=0 proof.
The optional `guest_smoke.py` uses normal runtime + WorkspaceService + real journal
checkpoint hooks but is **not HTTP or backend capture-pipeline coverage**. It is
now verified by parent execution producing guest-result.json (direct-service only).
Host-crash/ENOSPC/Redis-outage production scenarios still require deliberate live
evidence, not mocked tests. No unrelated libvirt domain is modified by this harness.

For this focused volume cleanup, test paths are
`runner/tests/test_docker_workspace_storage.py`, `test_docker_runtime.py`,
`test_storage_inventory.py` and `test_services_contract.py`. Actual test counts
belong in the completion report/run artifacts rather than cumulative phase notes.
No automatic pruning, generic garbage collector, journal reset, or broad foreign
resource deletion exists. For uncertain ownership preserve resources and inspect.


Booted Ubuntu direct-service verification subsequently passed in the isolated
parent run (`.opencuria/integration/booted-guest.log`,
`.opencuria/integration/live/guest-result.json`). This proves boot/SSH, injected
disposable credential scrub, standalone capture, reinjection and external-stop
proof refusal. The full real HTTP/backend-worker stop/capture/resume pipeline
subsequently passed parent verification (`live/guest-http-parent-result.json`):
completed CaptureRequest without diagnostic, running/unfenced workspace, ready
standalone QCOW2 checked clean, and ACKed terminal children. Registry-race and
resume credential acknowledgement fixes are covered by regression tests. The
opt-in `guest-http-poll` command rechecks that exact staged invocation without
issuing lifecycle actions; the corrected harness passed parent execution
(`live/guest-http-parent-result.json`). Capture resume resource regression
coverage confirms TaskRepository's existing eager preparation persists QEMU
1/1024/20 correctly; no production payload fix was necessary. Image-artifact GET
and MCP list no longer invoke legacy timeout mutation (independent recovery owns
progression).


### Liveness cache and heartbeat isolation

Transport heartbeats run independently of slow runtime/SSH observations. The
observation loop refreshes the canonical workspace cache and sends fresh states
with `workspace_states_observed=true`; cached transport heartbeats are not new
runtime evidence. Full storage scans retain their own sequence and completeness
contract. Stopped workspaces skip desktop/process guest probes. Immutable QEMU
publication hashes are cached only while device, inode, size, mtime and ctime
match; descriptor/path checks fence replacement races. Mutable workspace disks
and image-info/backing graph discovery are never satisfied from that hash cache.

### QEMU seed ownership and workspace removal

Read-only review of the real booted guest's `live/storage.json` found its exact
cloud-init ISO already observed and linked to its workspace, not an unknown
force-deletion dependency. The unmanifested `full-guest-base.qcow2` remains
intentionally unknown; this is not publication proof. Discovery now enumerates
ISO files too, but only the exact expected cloud-init path referenced by its
own domain receives seed ownership metadata; arbitrary/unreferenced ISO files
remain unknown. Before domain destruction and again before any disk unlink,
workspace removal checks reverse consumers of both the overlay and seed ISO.
A shared ISO or incomplete scan preserves the files. Targeted tests exercise
real qemu-img publication/overlay discovery with mocked libvirt domain+CD-ROM
XML through backend force policy, and standalone capture after origin removal.
