# Workspace and image lifecycle verification

The lifecycle uses durable commands and exact operation identities. A failure is
safe when it preserves the previous usable image, retains owned storage, and
leaves a retryable failure or a temporary automatic recovery fence. A timeout or lost
connection is never sufficient evidence to repeat an unknown destructive effect.

## Concurrency and resource admission

Workspace creation, resume, resource updates and capture/recreate allocation
recheck current runner limits under the runner lock. Capacity includes creating
guests, pending starts, unresolved operations and parent workflows between child
tasks. A pending or failed resource reduction retains the previous larger
reservation until a successful runtime result confirms the smaller configuration.
Stopped and retained failed guests continue to reserve disk space. Compute can
be released only after proven stop/absence; disk capacity needs proven absence
or deletion. Disk shrink is refused before settings change or guest shutdown.
Stale heartbeats cannot overwrite a newer lifecycle result or deletion state.

The runtime separately checks configured RAM for all active libvirt guests and
available host RAM before activation. It preserves a host margin of at least
1 GiB or 10% of total RAM. Build guests have their own bounded queue and CPU/RAM
settings; queued builds do not allocate VMs. Disk overlays remain sparse.
The standard recipe verifies its required desktop packages, commands and native
Python imports, and clears downloaded APT archives before flattening the image.

Each guest has a stable lifecycle lock. Storage graph mutations retain the
shared graph lock, while SSH/bootstrap readiness and graceful shutdown release
it. Repeated cancellation drains issued threads/subprocesses before releasing
their ownership. Disk creation and immutable image publication use exclusive
publication, so duplicate requests cannot truncate an existing disk.

Builds and newly created/cloned guests wait for cloud-init completion before
provisioning. Only the read-only readiness probe may reconnect/retry; the image
initialization script is never replayed. Published images carry their actual
virtual size and minimum disk size through callbacks and inventory. Backend
admission uses that minimum, and the runtime refuses a smaller target before
allocating resources. Clone selection verifies publication identity, size and
SHA-256 rather than accepting marker existence alone.

## Outages and recovery

SQLite journal reads and FULL-sync writes run through a serialized worker queue,
with cancellation drained until commit settles. Transport heartbeats use
in-memory identities independently of journal replay, storage scans and SSH.
The target remains fenced through terminal inspection and durable result commit.
Initial backend connection failure retries with bounded backoff; reconnect
preserves in-flight lifecycle work and replays unacknowledged results.

Recovery retries transient inspection failures and continues unrelated records.
Surviving disk helpers prevent a claim of quiescence; unreaped zombies do not.
Exact publication proof can recover a completed build/capture after a crash.
Unknown initialization is never repeated automatically. Once the runner seals
the identity and proves execution has ended with a complete quiescent scan, the
backend closes it as interrupted failure and releases the reservation. Ordinary
users can retry, recreate or delete without an administrator acknowledging the
interruption. Missing or contradictory proof keeps automatic recovery waiting;
it never guesses that effects have stopped.
Without durable allocation ownership, missing registry identity permits
idempotent removal only with exact physical absence proof, including disk, seed,
network and host-key resources.
For an interrupted allocation before domain definition, complete dependency
inspection can instead prove quiescence while retaining partial files and unknown
credential state. A fresh ordinary remove may use the newest exact durable
create/clone lineage to clean those resources. A later completed removal revokes
older lineage; known no-effect failures cannot grant allocation ownership.

A corrupt publication marker makes that image unusable, while a readable disk
header still permits following its complete backing graph. Unrelated cleanup
continues. Fresh, complete, uniquely associated inventory downgrades a known
corrupt ready image to failed. Corrupt or unreadable backing metadata still
blocks destructive work; location alone never grants ownership or deletion.

Backend recovery stages also run independently. A failed stage does not starve
other coordinators or durable delivery, but the tick still fails after making
progress so that the supervisor cannot report persistent failures as healthy.

## Backend state model and transitions

`Task.status` records execution (`pending`, `in_progress`, `completed`, `failed`).
The command phase records delivery and proof, not a second execution result:
`intent` → `delivery` → `executing` → `result`. An ambiguous operation temporarily
uses the legacy `intervention` phase while automatic inspection runs; sealed
interruptions end in `disposed` with a failed task. Failure message text no longer
creates an implicit recovery state. A never-prepared, never-issued local intent
fails and closes directly; an old delayed preparation cannot revive it.

Workspace `removed` was redundant with `deleted`: migration 0031 normalizes old
rows, and only `deleted` remains in the backend/API enum. Runtime `removed` maps
to `deleted`; transient runtime `removing` maps to `deleting`. Compatibility reads
still recognize imported legacy tombstones without authorizing physical deletion.

| Verified interruption | Result and next action |
| --- | --- |
| Create or clone, whether VM runs/stops/is absent | `failed`; initialization is unproven; recreate or delete |
| Start, stop, settings update, capture source | Fresh physical `running`/`stopped`, otherwise `failed`; retry or delete |
| Workspace deletion | `delete_failed`; retry deletion |
| Image build | Failed pending generation; previous current image preserved; build a new generation |
| Image deletion | `delete_failed`; bounded automatic idempotent retry after fresh inventory |
| Three proven deletion failures | Clear failed deletion request; an ordinary repeated delete request retries with the original approval |
| Known terminal outcome | Apply exact callback once; duplicate results do not alter newer operations |
| Runner offline, execution live, scan incomplete, identity/session mismatch | Wait for automatic verification; preserve ownership and resources |

Capture stop/capture/replace and recreation remove/create/stop phases remain
distinct because each has different side effects and credentials guarantees.
Image `ready`, `retired` and deletion phases retain selection/dependency semantics;
they cannot be merged safely. A changed destructive dependency graph still needs
renewed user approval, which is different from acknowledging an interrupted task.

Every immediate command records its issue counter and lease before emission,
matching recovery delivery. Fresh queue/execution heartbeats renew the two-hour
execution deadline. Never-issued work stays queued while offline; hardware queues
and slow live operations do not expire solely because two wall-clock hours pass.

The compact backend case catalog is
[`testing/lifecycle-backend-cases.md`](testing/lifecycle-backend-cases.md). Its
original expectations were archived before tests; later discoveries have an
explicit pre-execution addendum.

## Reproducible checks

Run the ordinary runner tests with its virtualenv. Backend lifecycle tests run
against isolated SQLite and PostgreSQL test databases; PostgreSQL is needed to
verify real independent sessions and row-lock contention.

The complete opt-in real VM matrix is:

```bash
runner/.venv/bin/python scripts/verify_lifecycle_state_matrix.py \
  --cloud-image /path/to/ubuntu-24.04-server-cloudimg-amd64.img \
  --distro 24.04 \
  --output /workspace/lifecycle-verification/ubuntu2404
```

Use `--distro 22.04` with the matching Ubuntu cloud image for the other supported
bootstrap. Choose a fresh output directory with a parent traversable by
`libvirt-qemu`. KVM, system libvirt, qemu-img, genisoimage and host networking
permissions are required. The script copies the supplied cloud image and keeps
its writable disks, publications, SSH key and journals below the output directory.
It removes only guests/networks allocated by that run and writes `report.json`.
Do not share its private SSH/journal files as public artifacts.

Additional real storage/admission and process-crash matrices are
`scripts/verify_workspace_faults.py` and
`scripts/verify_workspace_crash_recovery.py`. Both take `--cloud-image` and a
fresh `--output` directory. `scripts/verify_desktop_image.py` also takes
`--distro` and `--init-script` to build the complete rendered standard recipe and
verify its actual graphical desktop/browser and persistent storage. Run these
harnesses sequentially on a shared libvirt host; each matrix already exercises
parallel work internally. Output ancestors must be traversable by `libvirt-qemu`.

The compact catalogs and final evidence are consolidated in
[`testing/lifecycle-audit-report.md`](testing/lifecycle-audit-report.md).

| Scenario | Verification |
| --- | --- |
| Concurrent same/different image builds; one failed build | Real VMs and runtime regression tests |
| Definition image creation and captured-image cloning | Real Ubuntu 22.04/24.04 VMs |
| Parallel clone/resume, disk persistence and live resource update | Real VMs |
| Duplicate delivery and duplicate same-ID disk creation | Real VMs plus journal/admission race tests |
| Capture, source removal, backing dependency and image removal | Real VMs and real QCOW2 graph tests |
| Paused guest stop/resume and failed libvirt removal | Real VM stop plus runtime failure tests |
| Backend unavailable at startup and disconnected during execution | Real loopback Socket.IO server restart and offline completion/ACK replay |
| Runner SIGKILL after boot, before initialization completes | Real VM and separate runner subprocess |
| Published build/capture recovery after restart | Exact-operation publication tests using real QCOW2 files |
| Concurrent create/resume/update under CPU/RAM/disk limits | Independent SQLite/PostgreSQL sessions |
| Stale observations, out-of-order results and old runner sessions | Backend and runner regression tests |
| Slow fsync, repeated cancellation and interrupted journal admission | Thread-gated IO tests with responsive event-loop/heartbeat assertions |
| Invalid manifests, corrupt storage, incomplete scans, ENOSPC | Real QCOW2 tools with injected inspection/publication failures |
| Surviving helpers, zombies and delayed duplicate delivery | Runner recovery and real subprocess tests |

The lifecycle matrix uses small initialization scripts; the separate desktop
checks build the complete standard recipe on both Ubuntu releases. Arbitrary
custom recipes are not exhaustively covered. Physical host power loss, SSD controller failure and
every possible external guest modification are not reproduced by these tests.
Process termination and injected IO faults test the software boundaries; they
cannot establish an absolute guarantee about arbitrary hardware or workloads.
