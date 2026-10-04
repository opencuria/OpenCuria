# Live image/storage integration harness

Opt-in, staged tests; **no production feature changes and no mocks**. Run from
`/workspace/OpenCuria`. Requires the dedicated empty PostgreSQL database,
localhost Redis, Docker daemon, `qemu-img`, system libvirt, and existing component
venvs (runner venv includes `requirements-qemu.txt`). Never run on a production
DB. Nothing here flushes Redis, prunes Docker, or enumerates domains for deletion.
The scripts do not start/stop backend processes; the parent owns those processes.

## Environment and migration

Export these variables in **every parent process** (process tools do not inherit
exports from a previous shell tool call). Do not put passwords/tokens in tracked
files. Use a newly generated runner token for each run. Database credentials are
supplied by the parent; they are not hard-coded in this harness.

```bash
cd /workspace/OpenCuria
export PYTHONPATH="$PWD/backend:$PWD/e2e/fixtures"
export DJANGO_SETTINGS_MODULE=integration.settings
export INTEGRATION_DB_NAME=opencuria_integration
export INTEGRATION_DB_USER=opencuria_test
export INTEGRATION_DB_HOST=127.0.0.1
export INTEGRATION_DB_PORT=5432
export INTEGRATION_DB_PASSWORD='<dedicated database password>'
export INTEGRATION_ADMIN_PASSWORD='<isolated test login password>'
export RUNNER_API_TOKEN="$(openssl rand -hex 32)"
export INTEGRATION_RUN_DIR="/workspace/.opencuria/integration/$(date +%Y%m%d-%H%M%S)-$(openssl rand -hex 4)"
export INTEGRATION_BACKEND_URL=http://127.0.0.1:8011
export REDIS_URL=redis://127.0.0.1:6379/15
export RUNNER_BACKEND_URL=http://127.0.0.1:8011
export RUNNER_ENABLED_RUNTIMES=docker,qemu
export RUNNER_STATE_DIR="$INTEGRATION_RUN_DIR/runner-state"
export RUNNER_QEMU_IMAGE_CACHE_DIR="$INTEGRATION_RUN_DIR/qemu/images"
export RUNNER_QEMU_DISK_DIR="$INTEGRATION_RUN_DIR/qemu/disks"
export RUNNER_QEMU_SNAPSHOT_DIR="$INTEGRATION_RUN_DIR/qemu/snapshots"
export RUNNER_QEMU_SSH_KEY_PATH="$INTEGRATION_RUN_DIR/qemu/runner_key"
export RUNNER_INVENTORY_INTERVAL=5
export RUNNER_HEARTBEAT_INTERVAL=5
mkdir -p "$RUNNER_QEMU_IMAGE_CACHE_DIR" "$RUNNER_QEMU_DISK_DIR" "$RUNNER_QEMU_SNAPSHOT_DIR"
chmod 700 "$INTEGRATION_RUN_DIR"
backend/.venv/bin/python backend/manage.py migrate --noinput
backend/.venv/bin/python e2e/integration/fixture.py seed
```

Redis channel `opencuria-runner` is fixed in production code (Redis DB numbers do
NOT isolate pub/sub). Do not run a second OpenCuria backend/worker sharing this
Redis while exercising the harness. Ordinary unrelated Redis users are preserved;
no Redis cleanup or flush is needed. PostgreSQL overlay rejects other DB names.

## Start processes using the parent's process tools

Separate processes, appropriate working directory, full environment above:

* **Backend**, cwd `backend`:
  `.venv/bin/uvicorn config.asgi:application --host 127.0.0.1 --port 8011 --workers 1 --lifespan on --ws websockets-sansio --ws-max-size 209715200`
* **Independent durable worker**, cwd `backend`:
  `.venv/bin/python manage.py recover_lifecycle`
* **Runner**, cwd `runner`: `.venv/bin/python -m src`
* **Frontend**, cwd `webapp`:
  `VITE_WS_BASE_URL=http://127.0.0.1:8011 npm run dev -- --host 127.0.0.1 --config ../e2e/fixtures/integration/vite.config.ts`

The frontend fixture redirects `/api` and desktop/runner proxies to 8011; the
explicit WS base also directs frontend Socket.IO to the backend. Existing Vite
and production settings are untouched. One ASGI worker only; lifecycle recovery
is deliberately a separate worker.

## Docker HTTP lifecycle and browser fixtures

```bash
backend/.venv/bin/python e2e/integration/http_smoke.py login
backend/.venv/bin/python e2e/integration/http_smoke.py build
backend/.venv/bin/python e2e/integration/fixture.py attach
# Restart ONLY this run's runner now: rebuild its normal runtime cache after
# manually attaching the lower-level containers. Parent process_restart.
backend/.venv/bin/python e2e/integration/http_smoke.py verify
```

`build` uses the normal recipe renderer and actual Docker daemon, with
`alpine:3.21`, no packages, one tiny `RUN` instruction (no apt/KasmVNC). Actual
recipe revisions and immutable generations are allocated by HTTP/worker. `attach`
creates one stopped and one running real container, pinned to the first image
identity. These are clearly labelled **test fixtures**, not successful agent
provisions. `verify` checks observed dependency edges, owner metadata, exact
physical ID, invalid force fingerprint rejection (409), deferred no-child state,
cancellation and changed-recipe rebuild, preserving the original consumers.

Run these stages once per seed; `storage`, `login`, `poll` are repeatable.
Raw responses, previews, IDs and inventory snapshots are written below the run
directory. All JSON files are mode 0600, root run directory mode 0700. The password
is supplied by env; `auth.json` contains real JWTs for browser verification. Do
not commit these artifacts. Browser URL: **http://127.0.0.1:5177**, login email
**images-admin@example.test**, password from `INTEGRATION_ADMIN_PASSWORD`.
Select **Runner Image Verification**. Verify Images/Runners storage against
`storage.json`, not hand-entered sizes. Screenshots are captured by the parent.

## Real QEMU fixture (no guest OS)

```bash
runner/.venv/bin/python e2e/integration/qemu_fixture.py prepare
backend/.venv/bin/python e2e/integration/fixture.py attach-qemu
# Restart ONLY this run's runner to discover stopped fixture domains.
backend/.venv/bin/python e2e/integration/http_smoke.py storage
```

A newly created empty 16 MiB virtual disk, defined but never booted, provides an
honest lower-level credential-clean proof. The test first proves capture refuses
missing proof, then invokes the real runtime capture, checks actual file size,
self-contained QCOW2 and integrity. A real stopped dependent libvirt domain and
QCOW2 backing overlay cause deletion to fail. The script leaves them available
for browser inventory. `attach-qemu` registers those exact UUIDs/paths/sizes in DB;
it never writes cached inventory or fictional metrics. Shared libvirt is read
for inventory only; no existing domain is changed or started.

This deliberately does **not** test a booted QEMU guest, SSH readiness, guest
credential scrub, full API capture orchestration or resume. Blank disks must not
be offered as usable provisioned guest images. The source is explicitly named
“no guest OS”. A full guest test requires separately provisioned isolated Ubuntu
and real scrub evidence; do not claim that coverage from this test.

## Force deletion and reconnect/replay

After taking browser screenshots, normal force run:

```bash
backend/.venv/bin/python e2e/integration/http_smoke.py force
backend/.venv/bin/python e2e/integration/http_smoke.py poll
```

`force` reads a fresh **definition** cascade, requires zero blockers and exactly
the run's Docker consumers, and sends its structural fingerprint. `poll` requires
completed outcome, late cancel rejection (409), actual container/image absence,
and one terminal, acknowledged journal record per child UUID. QEMU artifacts are
not in this Docker definition cascade. Inventory is re-requested between steps;
unknown/incomplete scans are failures, never zero-byte success.

For a **deliberate lost-backend-outcome** scenario, run after `verify` and before
`force`. The ASGI process owns the runner socket: once ASGI is stopped, Redis
cannot deliver new commands to that socket. Work must already be executing.
Parent controls the process timing; the harness does not kill/restart anything.

1. With backend, worker and runner online, stage a real slow rebuild:
   `backend/.venv/bin/python e2e/integration/http_smoke.py slow-build`.
   It patches the existing run-owned definition with a unique Alpine
   `RUN sleep 12 && echo <unique marker> > /fixture`, defeating layer cache,
   dispatches rebuild, records task/generation in private `slow-build.json`,
   and returns immediately **without awaiting successful completion**.
2. Confirm that exact task has reached the runner journal (`result IS NULL`)
   and Docker build is actually in progress using runner logs/Docker build
   activity. Run `http_smoke.py snapshot` to preserve read-only journal evidence.
   A backend pending row alone is not proof the runner received the command.
3. Stop ONLY this run's ASGI **while that Docker build is executing**. Keep the
   runner alive to finish its existing work. Parent may stop the worker to avoid
   distracting retry noise, but it cannot deliver new work while ASGI is absent.
4. While ASGI is still down, repeat `http_smoke.py snapshot` until the **same
   recorded task** has `event="image:built"`, `result IS NOT NULL`, `ack=0`.
   Preserve this snapshot and actual Docker image publication evidence. If it
   was already acknowledged before shutdown, or no work finished, the intended
   loss window was missed: report inconclusive and retry `slow-build` (unique
   instruction prevents cache hits). Twelve seconds is deliberately short;
   act promptly and never infer replay from a normal successful build.
5. Restart ONLY this run's ASGI and the worker if stopped, preserving runner
   state. Runner reconnect replays its real persisted outcome. Run
   `backend/.venv/bin/python e2e/integration/http_smoke.py slow-poll`.
   It requires the exact recorded generation to become current, the same task
   to have one `image:built` acknowledged journal row, and one actual inventory
   image with the matching operation label. Preserve before/after snapshots.
6. After screenshots, `force` / `poll` remove the definition cascade including
   this extra generation. Do not run `force` during a pending rebuild.

This checks convergence and stable execution identity, not merely delivery of a
new task after restart. Actual pre-restart terminal ack=0 evidence is required
for claiming lost-outcome replay coverage. Nothing fakes the runtime or network.

## Cleanup / limitations

Stop ONLY the parent-owned runner before QEMU cleanup (avoid concurrent scans):

```bash
runner/.venv/bin/python e2e/integration/qemu_fixture.py cleanup
backend/.venv/bin/python e2e/integration/fixture.py cleanup
```

QEMU cleanup checks exact UUID, shut-off state and recorded disk path before
undefining each owned domain; deletes overlay first; real runtime then proves
capture deletion succeeds and bytes/manifest are absent. A changed/active domain
is refused, never forcibly destroyed. Docker cleanup checks per-run labels.
Successful HTTP force removes owned Docker images through normal runtime code.
On failed builds inspect recorded definition generations and delete only exact
owned UUID tags, not `docker system prune`. Base distro layers are intentionally
left as shared cache. No user DB rows are bulk deleted: retain dedicated DB
history for evidence; recreate only this dedicated database when explicitly
approved. Cleaned QEMU DB rows may remain stale evidence until next observed
inventory; do not pretend physical cleanup was an API deletion.

Stop backend, worker and frontend through their own parent process IDs. Retain
private journal/auth artifacts until evidence is collected; then remove only the
exact run directory, after verifying no run-owned disk/domain remains.

## Canonical Docker data-volume smoke

```bash
# runner Python; actual Docker create/mount/shared-consumer/delete/repeat checks
# Requires alpine:3.21 already pulled (or INTEGRATION_DOCKER_IMAGE).
runner/.venv/bin/python e2e/integration/docker_volume_smoke.py
```

This exercises normal Docker runtime creation with a real `/workspace` volume;
previous manually attached fixtures without mounts cannot prove data deletion.
No bash/injection bootstrap is invoked (Alpine lacks bash). Evidence is in
`docker-volume*.json`. Failures leave exact UUID/name for investigation; never prune.

## Optional booted Ubuntu service smoke (parent-run, not HTTP)

`guest_smoke.py` is authored separately; **not claimed tested**. It uses normal
QemuRuntime + WorkspaceService + WebSocketInterface checkpoint/journal wiring,
without connecting a second runner socket or sharing the live daemon journal.
It is not full HTTP capture-coordinator/replay coverage. The parent must run it
and retain evidence before claiming boot, SSH, controlled credential scrub,
standalone capture, resume and external-stop observation.

Use a fresh UUID parent path, and do NOT change the live runner's environment.
The existing pristine public cloud image is read only. The script copies it with
qemu-img convert; it never resizes/writes the source. Only generated fixture SSH
keys and a parent-supplied controlled credential are used. Avoid real secrets.

```bash
export GUEST_ROOT="/var/lib/libvirt/images/opencuria-integration/$(uuidgen)"
export RUNNER_QEMU_IMAGE_CACHE_DIR="$GUEST_ROOT/images"
export RUNNER_QEMU_DISK_DIR="$GUEST_ROOT/disks"
export RUNNER_QEMU_SNAPSHOT_DIR="$GUEST_ROOT/snapshots"
export RUNNER_QEMU_SSH_KEY_PATH="$GUEST_ROOT/runner_key"
mkdir -p "$GUEST_ROOT"/{images,disks,snapshots}
# Parent: discover exact libvirt QEMU service account on this host. Grant only
# traversal/read-write on this NEW dedicated guest chain with setfacl (including
# default ACL on the 3 new directories for disks created later). Preserve source
# permissions and unrelated /workspace run dir mode 0700. No chmod -R 777,
# no seclabel disabling. Ubuntu host commonly uses libvirt-qemu; verify first.
# Example after verifying account:
# setfacl -m u:libvirt-qemu:x /var/lib/libvirt/images/opencuria-integration
# setfacl -m u:libvirt-qemu:rx "$GUEST_ROOT"
# setfacl -m u:libvirt-qemu:rwx,d:u:libvirt-qemu:rwx "$GUEST_ROOT"/{images,disks,snapshots}
export INTEGRATION_GUEST_TEST_CREDENTIAL='<controlled disposable test value>'
runner/.venv/bin/python e2e/integration/guest_smoke.py prepare-guest
runner/.venv/bin/python e2e/integration/guest_smoke.py guest-smoke
# Inspect guest-result.json, exact virsh domain UUID, qemu-img evidence.
runner/.venv/bin/python e2e/integration/guest_smoke.py cleanup
unset INTEGRATION_GUEST_TEST_CREDENTIAL
```

Run cleanup only when parent is ready to delete this exact fixture. Runtime
reverse-dependency checks still apply. Failure preserves UUID/base paths in
`guest.json`; fix ACLs/readiness on that fixture, do not repeatedly invoke create.
No existing guests, networks or base images are deleted. The 20 GiB disk is thin,
1 vCPU / 1024 MiB guest is small but may need several minutes for SSH/cloud-init.
For complete HTTP guest tests, register an explicitly legacy pristine base in the
isolated DB and create through normal API; this script does not fabricate a ready
HTTP artifact or pretend direct-service calls covered backend orchestration.

## Completed parent verification / remaining scope

Parent executed successfully in the isolated environment:

- PostgreSQL migration/repository/recovery: 80 passing tests including four
  real independent-connection concurrency tests; cancellation verification:
  55 passing tests.
- Real tiny Docker HTTP build/rebuild, preserved old pins, deferred cancel,
  approved force cleanup of all fixture containers and three image generations;
  every child journal ACK confirmed.
- Actual backend outage during a 12-second Docker build: the exact task had
  terminal image:built ACK=0 before restart (`outage-before.json`), then ACK=1
  after reconnect, with exactly one published generation. No running container
  was stopped for this outage.
- `docker_volume_smoke.py` passed with actual data, inventory size, shared
  consumer blocker and confirmed volume/network absence.
- Real stopped blank QCOW2/libvirt capture/dependency checks passed.

Booted `guest_smoke.py` passed parent execution on the real Ubuntu guest:
`/workspace/.opencuria/integration/booted-guest.log` and
`/workspace/.opencuria/integration/live/guest-result.json`. This is direct-service
coverage; the subsequent real HTTP coordinator pass is recorded below. Both resume paths now
supply explicit 1 vCPU / 1024 MiB / 20 GiB settings. It checks the actual installed
credential env and profile files before stop, both files/environment block absent
on credential-free resume, and completed libvirt shutoff before accepting scrub
proof. No credential values are printed. Global inventory may observe unrelated
foreign domains read-only; neither smoke nor cleanup changes those domains.


## Opt-in existing booted guest HTTP coordinator verification

Do not rerun prepare/create or cleanup first. The direct smoke leaves the exact
recorded guest externally stopped with disposable injected credentials on disk.
The parent must source its live backend/auth environment plus `guest-env`, then:

```bash
# Only the parent's own live runner is restarted, using guest-env directories.
# Preserve RUNNER_STATE_DIR="$INTEGRATION_RUN_DIR/runner-state" (not guest-journal).
export RUNNER_ENABLED_RUNTIMES=qemu
backend/.venv/bin/python e2e/integration/fixture.py attach-booted-guest
# Parent: restart its own runner process with these env settings, await connection.
backend/.venv/bin/python e2e/integration/http_smoke.py guest-http-pipeline
```

Attachment verifies exact libvirt shutoff/disk path and registers the real
pristine base as a legacy import and the existing guest with resources 1/1024/20,
owner from the manifest, credentials-present true. It does not seed inventory,
mutate the domain, or claim the base was a managed capture/build. The HTTP command
resumes with empty credentials, then requests explicitly approved stop/capture/
resume. It requires running/unfenced state, ready new artifact, a completed
CaptureRequest with no restart-failed diagnostic, all four persisted commands and
ACKed real journal results, and a checked self-contained QCOW2. Evidence is
`guest-http-staged.json` and `guest-http-result.json`. Parent independently verified
the full real HTTP pipeline in `guest-http-parent-result.json`: completed capture
without diagnostic, running/unfenced guest, ready standalone image, clean qemu-img
check, and ACKed stop/capture/resume children. Nothing invokes this command by default.
No live harness commands were executed when authoring this addition.

If the first HTTP resume completed but the immediate credential assertion failed,
restart **only the parent-owned backend** to load the resume acknowledgement fix.
Then POST `/api/v1/workspaces/<recorded-workspace-id>/stop/` through authenticated
HTTP and await stopped, no active operation, no intervention, and credentials
absent before rerunning `guest-http-pipeline`. Keep the same runner journal and
fixture; do not reattach or manufacture DB credential state. The harness still
requires a stopped guest, accepts either observed credential boolean at entry,
and verifies only the four commands allocated by this invocation (prior genuine
resume/stop commands remain preserved). The subsequent parent run passed after the registry-race and resume credential
acknowledgement fixes; their regression tests preserve these contracts.

Cleanup remains parent-owned: normal HTTP DELETE of the exact guest is allowed
(the captured image has independent provenance), followed by graph-approved
image/base deletion as appropriate. Keep the direct smoke's captured UUID and
base paths for its exact lower-level cleanup; do not prune or touch foreign guests.


### Recheck the staged invocation without dispatching new work

```bash
# Same dedicated backend/auth environment, run directory and runner journal.
backend/.venv/bin/python e2e/integration/http_smoke.py guest-http-poll
```

This mode issues GETs and read-only DB/journal/file checks only; it never resumes,
stops, captures, or refreshes inventory. It preserves existing staged responses,
persists the selected request ID, and scopes the four task assertions from that
staged initial resume through that request's final resume. Initial selection uses
the exact returned stop task, not all captures for the workspace. For older stage
files already advanced beyond stop, it requires a unique request allocated between
the persisted initial resume and exact stop task timestamps, refusing ambiguity.
Physical verification prefers the image-linked inventory resource and confines it
to the recorded snapshot root; otherwise it resolves the sanitized image UUID
there. A runner reference UUID is never passed as a filesystem path.

Remaining live limits: host-crash/ENOSPC/Redis-outage behavior has not been exercised.
The corrected polling command still needs parent execution to establish its own
harness pass; the underlying full HTTP pipeline already passed independently.
