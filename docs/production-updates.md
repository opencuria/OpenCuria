# Production updates (opt-in)

## Supported boundary

The managed topology is for one Linux Docker Compose host, PostgreSQL 16 and
Redis. Ordinary **compatible API and harness worker releases** coexist: a run
stays on its original executable version, including subagents and pending user
questions/permissions. New roots are admitted to an explicitly compatible ready
worker. The stable Socket.IO/desktop gateway, scheduler and recovery dispatcher
are separate from the versioned HTTP/UI projects.

Runner supervised mode likewise keeps one Socket.IO identity and one exclusive
operation journal while locally staged immutable source generations coexist.
Existing PTYs, process streams and desktop routes retain their original runner
generation. A browser reconnect attaches the same PTY and replays bounded,
sequenced output instead of creating a replacement shell. Input IDs prevent
repeating confirmed stdin within the retained deduplication window. Unconfirmed
input is never automatically replayed.

This is **not** arbitrary live Python module replacement, a durable LLM stream
checkpoint engine, or host-level HA. The first conversion, supervisor/runtime
Python dependency changes, incompatible protocols and destructive schema changes
require explicit maintenance. Do not force-stop a supervisor or an old worker
that owns work. QEMU guest/kernel upgrades and database major upgrades are not
included in this application rollout contract.

## Durable execution and failure semantics

- PostgreSQL commits session admission, user/assistant messages, run ownership and
  the targeted start outbox in one transaction. Exactly one unresolved run per
  session is allowed. The worker claims a start once; claimed work is not replayed
  after a process crash. Each actual worker process uses a fresh identity, even
  after a container restart.
- A short database/Redis outage is not proof that a run process died. No automatic
  takeover is performed merely because a heartbeat expired. Old runs stay fenced
  against competing runs until settlement or explicit recovery.
- Permission/question answers remain in their existing stores; the owner polls
  committed answers. Abort intent is durable. Commands contain identifiers, not
  duplicate prompts/credentials. Critical tool timeline persistence failures stop
  execution and retain unresolved ownership rather than running an unrecorded tool.
- Worker runner requests and replies are routed by exact run token, worker,
  release, root, workspace and runner identity. Redis pub/sub carries transient
  bytes; metadata routes are bounded/expiring and renewed for quiet live streams.
  Stream acknowledgements reflect acceptance by the actual consuming worker.
- Shell/MCP effects have no general exactly-once guarantee after a crash. Never
  blindly retry an unknown effect. Native recovery requires both independently
  proven worker death and confirmed runner quiescence; external engine recovery
  additionally requires confirmed lease/process cleanup. Unknown ownership stays
  busy/closing and blocks overlapping admission.
- Terminal output retention is bounded (1 MiB / 4096 chunks). An expired replay
  cursor reports an explicit error; it never silently pretends the transcript is
  complete and never replaces the live PTY to hide a gap.

The stable services and Redis are trusted internal infrastructure. Do not expose
Redis, the Docker socket or runner IPC publicly. Back up the database, credential
encryption key, runner state and workspace volumes consistently.

## Release manifest and compatibility

Copy `release.json.example` to a release-specific immutable manifest. The same
six metadata values must be configured in the process environment:

```json
{
  "version": "1.2.3",
  "protocol_min": 1,
  "protocol_max": 1,
  "database_schema": 1,
  "database_schema_min": 1,
  "database_schema_max": 2
}
```

Release version is an immutable build tag, not `latest`. The deployment script
currently accepts tags only; enforce tag immutability in your registry. Protocol
ranges must intersect across **every active, draining and rollback-retained
release**, not merely N/N-1. The candidate database schema must be readable and
writable by all retained binaries **before** any DDL runs. Registered metadata is
immutable. A live run/worker whose release inventory is missing or retired blocks
online migration. Keep stable control-plane versions registered as well.

`OPENCURIA_GATEWAY_WORKER_VERSIONS` is the explicit set of compatible worker
releases to which a gateway/scheduler may admit roots. Existing runs do not need
re-admission and retain their owner. Drain the old release after cutover so the
stable scheduler can fall back to a newer compatible ready generation. Retain
old images for the whole drain and rollback window.

## Initial installation / conversion (maintenance)

1. Drain the legacy installation, including agent runs, MCP sessions, terminals
   and viewers. Back up its data. Do not infer successful drain from `busy` flags
   or process-local task maps alone.
2. Configure `.env` from `.env.upgrade.example`, `.env.operations` from
   `.env.operations.example`, and an exact manifest for each retained version.
   Keep secrets out of Git. Supply a real TLS terminator in front of the proxy;
   the Compose proxy is HTTP on a private host port, not public TLS termination.
3. Create the stable infrastructure:

   ```sh
   docker compose --env-file .env -p opencuria-infra \
     -f compose.upgrade.infra.yml up -d --wait db redis proxy
   ```

4. On a **fresh empty DB only**, run the dedicated migration container with
   `OPENCURIA_UPGRADE_ARGS=--allow-initial-maintenance-bootstrap` and the exact
   candidate tag/version/environment. This flag is rejected on an existing DB.
   For an existing legacy DB, use an explicit maintenance `manage.py migrate`
   first, then `upgrade_database` with the exact
   `--adopt-existing-release-manifest`; do not combine inventory adoption with
   a new release cutover. Verify the adopted schema/manifest before proceeding.
5. Register the stable operations release with `record_release register` using
   its exact manifest; start stable operations separately:

   ```sh
   docker compose --env-file .env.operations -p opencuria-operations \
     -f compose.operations.yml up -d --wait
   ```

6. Enable runner supervised mode only after the initial maintenance drain:
   `RUNNER_SUPERVISED_MODE=true python -m src serve`. Preserve `RUNNER_STATE_DIR`.
   Run the first versioned app rollout below. All processes must share the same
   PostgreSQL/Redis/credential key settings, not the legacy local SQLite DB.

The stable public Nginx starts with disabled upstreams. Its directory-mounted
configuration supports atomic include replacement; binding only the individual
include file would keep the old inode after rename and is incorrect.

## Compatible rollout

Use a **new project suffix for every generation**, allowing more than two
concurrent releases when old runs last longer than the release interval:

```sh
# .env metadata and release.json describe this exact immutable candidate.
sudo ./scripts/deploy-production.sh 1.2.3 r123
# Later; old r123 remains running.
sudo ./scripts/deploy-production.sh 1.2.4 r124
```

The script locks deployment, refuses to recreate an existing worker project,
pulls images, copies a per-release manifest, runs checked online migrations,
starts candidate API/UI/worker, waits for DB/worker/scheduler/recovery and HTTP
readiness, then validates and gracefully reloads the public proxy. It does not
restart stable infrastructure or operations, stop old containers, reverse the DB,
or apply a drain deadline. Failure retains the previous upstream and candidate
for diagnosis. A failed Nginx reload restores the previous include.

After cutover, explicitly fence new roots on the old release:

```sh
# Use a current compatible API container. --once initiates drain without waiting.
python manage.py drain_release --release-version 1.2.3 --once
# Omit --once to observe until runs/gates and worker processes have drained.
python manage.py drain_release --release-version 1.2.3
python manage.py record_release rollback --release-version 1.2.3
# Once actual processes are gone, runs/gates settled, and rollback retention ended:
python manage.py record_release retire --release-version 1.2.3
```

Stopped worker audit records are retained; they are not live execution. Retiring
never removes schema or guest state. Stop/remove obsolete API/UI containers only
after their HTTP requests drain. Never use `compose down`, `down -v`, or a timed
forced worker stop as a normal update step. An indefinite gate can legitimately
retain an old release; use declared maintenance for urgent security fixes.

Application rollback means selecting an already retained, healthy compatible
API/UI upstream and routing **new** runs to compatible ready workers. Do not
resume a stopped process identity, replay its old commands, reverse migrations,
or restore a database backup over live writes. Draining/retired releases do not
accept new roots. Re-register a compatible retained release only deliberately,
with new worker processes and exact metadata, after checking its current schema.

## Runner code generation rollout

```sh
python -m src stage-generation /srv/runner-release-1.2.4 runner-1.2.4 --stage-only
python -m src stage-generation /srv/runner-release-1.2.5 runner-1.2.5
python -m src upgrade-status
```

Staging copies source locally, rejects symlinks/duplicate version IDs and stores a
content digest. Activation verifies the digest, authenticated child PID/UID and
required capabilities before switching new work. Dependencies/interpreter remain
shared and must be compatible; dependency changes are maintenance, not source-only
rollouts. Do not replace the supervisor container to update its child source.
Old children are retired only when their tracked sessions and work have ended.
A child crash with live pins does not authorize replay on another child.

## Online migrations

`python manage.py upgrade_database --manifest /path/release.json --plan-only`
validates without applying migration work. The real command takes a stable
PostgreSQL session advisory lock and a short DDL lock timeout, checks all retained
versions against the target schema, and applies only an allowlisted migration
plan. Django migration transactions remain intact. A failure does not downgrade
or fake-apply anything.

Use **expand → bounded/idempotent backfill → switch → contract later**. Fresh
tables and nullable non-indexed scalar columns are generally allowed. A Python
field default alone does **not** make a new NOT NULL column compatible with old
writers; that is conservatively refused. Relation/index/constraint changes,
destructive operations and opaque SQL/Python fail closed unless an opaque
migration has a specific audited rationale. Use `AddIndexConcurrently` only with
`atomic=False`; a failed build may leave an invalid index requiring inspection
and exact `DROP INDEX CONCURRENTLY` before retry. Contract/destructive schema
changes currently require a reviewed maintenance release; there is no automatic
bypass based only on a schema counter or a green empty-database test.

## Crash recovery and maintenance

Prove the exact process is dead and mark its registration stopped/failed before
running `recover_harness_worker WORKER_ID --confirm-worker-dead`. For native
attempts also verify/cancel all runner operations and pass
`--confirm-runner-quiescent`. This is an operator attestation, not a guarantee
inferred from a timeout. Unclear effects are not reissued. Preserve all journal,
lease and guest control records until reconciliation is complete.

Stable Socket.IO gateway/supervisor/Redis failures can interrupt active byte
streams; automatic continuation across arbitrary stable-plane crashes is **not**
claimed. Source worker/API updates deliberately keep this plane alive. A single
server remains a failure domain. Treat changes to this plane as explicit
maintenance with a verified drain and backup/rollback plan.

## Verification

Run the full project commit gate and PostgreSQL concurrency/migration suites.
The opt-in runner test uses a real isolated Docker daemon and two actual worker
processes, verifies the same PTY PID/environment after activation, reattach,
stdin ACK and duplicate-input suppression:

```sh
cd runner
OPENCURIA_RUNNER_LIVE_DOCKER_URL=unix:///tmp/test-docker.sock \
  .venv/bin/python -m pytest -q tests/test_supervised_live.py
```

Never point this destructive test fixture at a production Docker daemon. It
creates/removes only its own labelled test container, but runtime inventory sees
that daemon. See the verification report for which environments/flows were
actually exercised; passing unit tests alone is not a production HA certificate.
