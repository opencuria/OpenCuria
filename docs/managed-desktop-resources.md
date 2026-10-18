# Managed desktop resources

Managed desktops are declared dependencies, not an implicit side effect of
launching an MCP server. The backend owns plugin policy and run ownership; the
runner owns display bindings, leases, and workspace processes. Deploy matching
backend, runner, and webapp versions together.

## Plugin configuration

Each entry in a plugin's `mcp_servers` supports `resources.desktop`. This is
**stdio-only**: HTTP/SSE servers cannot request a workspace-local managed display.
Omitting `resources`, or using `{}`, means no managed desktop dependency.
Unknown resource keys and activation values are rejected.

```json
{
  "name": "Playwright",
  "slug": "playwright",
  "transport": "stdio",
  "command": "npx",
  "args": [
    "-y", "@playwright/mcp@latest", "--isolated", "--no-sandbox",
    "--executable-path", "/usr/bin/google-chrome-stable",
    "--output-dir", "/workspace/.opencuria/playwright"
  ],
  "cwd": "/workspace",
  "env": {},
  "resources": {"desktop": {"activation": "first_tool"}},
  "startup_timeout_seconds": 120,
  "request_timeout_seconds": 60
}
```

This is a nested MCP server definition, not a standalone REST request. Use it in
`mcp_servers` when creating or updating a plugin, or edit the server in
**Settings → Plugins**. Preserve other server definitions when replacing that
list. Plugin activation still requires both organization and workspace activation.

Activation policies:

- `server_start` (the default when `desktop` is declared without an activation):
  reserve a binding and hold the desktop before launching the server. Use this
  when initialization itself needs X11.
- `first_tool`: reserve and inject the binding before server launch, but hold the
  display only before the first tool call. Discovery/initialization does not
  activate the display. The hold lasts for the connection, not just that call.
  Only use this for servers that tolerate an inactive display at initialization.

The standard seeded Playwright plugin uses **`first_tool`**, headed Chromium,
`env: {}`, and the workspace artifact directory shown above. This is resource
policy, not a Playwright-specific runner capability.

The runner supplies `DISPLAY` and `XAUTHORITY` from its binding. A managed desktop
combined with either explicit environment key is rejected: remove both keys or
remove the managed declaration. Stdio launch no longer injects an implicit
`DISPLAY` into undeclared servers. There is **no legacy compatibility fallback**.
An unmanaged server may retain explicit environment settings, but receives no
lease, activation, renewal, or managed cleanup guarantees.

### Migration and manual adoption

Backend migration `plugins.0009_managed_desktop_resources` adds the resources
field and changes only the untouched global Playwright server seed. Its guard
matches the prior command, args, environment, cwd, transport, URL, headers, auth
settings, timeouts, and empty resources. It replaces the old static display env
with `{}` and the `first_tool` declaration. Original matching global skill and
plugin description text are updated independently to say headed Chromium.

Customized global servers and organization-owned copies are **not** automatically
converted. Audit all custom desktop-dependent stdio plugins: remove explicit
`DISPLAY`/`XAUTHORITY`, declare `resources.desktop`, and select the policy that
matches their startup behavior. Do not assume a successful migration converted
every Playwright copy. Migration reversal does not restore the seed data changes.

## Ownership and lifetime

The runner understands generic lease kinds: `viewer`, `computeruse`, and `mcp`.
A lease carries workspace/incarnation identity, owner, runner epoch, revision,
and state. Binding/reservation alone does not start Xvnc; a held lease activates
it. Releasing one owner does not stop a display held by another owner.

Owners renew every **45 seconds**; runner TTL is **180 seconds**. The runner
sweeper runs every **15 seconds** by default (`RUNNER_DESKTOP_LEASE_INTERVAL`).
Expiry enters fenced cleanup rather than silently transferring ownership.
Release tombstones prevent delayed reserve/launch operations from resurrecting
ended owners. Renewal does not create a missing or ended lease.

MCP and computer-use ownership is run-scoped. A renewal rejection or runner epoch
change fails closed and aborts the affected attempt; it must not continue using
an old display binding. Restart recovery closes old-epoch owners and their
managed processes. It does **not transparently resume** their runs or transports.
A new attempt needs fresh ownership. Cleanup is eventual **only once the guest
and runtime are reachable and process closure can be verified**. Unreachable or
uncertain cleanup stays protective/closing and is retried, not reported as done.
TTL and sweeper intervals are not a guaranteed cleanup deadline.

## Durable state and cleanup scope

Persist the runner's entire `RUNNER_STATE_DIR` across restarts, alongside the
workspace disks/volumes. The default for native runners is
`~/.local/share/opencuria/runner`; Compose configures `/var/lib/opencuria/runner`
(the remote runner Compose file mounts `runner_state`). Never share this state
between runner identities or delete it to clear a stuck operation.

State includes the lifecycle journal `operations.sqlite3`, desktop/recording
ownership in `desktop_leases.sqlite3`, and managed stream associations and owner
tombstones in `stream_intents.sqlite3`. The desktop/stream stores contain cleanup
metadata, **not command argv, rendered environment, credential values, or
authentication tokens**. Physical guest incarnation tokens are non-secret
identity evidence, not credentials.
Guest control records likewise contain process identity and close tombstones,
not secrets. This is not a claim that arbitrary workspace files or application
artifacts cannot contain secrets; protect workspace backups accordingly.

### Retention and replay fences

Terminal lease/recording records, closed stream associations, ended-owner
records, and guest close tombstones are deliberately retained **indefinitely**.
They are replay fences, not disposable logs. Ordinary stop/removal does not
justify forgetting them: a logical workspace/viewer identity can be reused and
old reserve, launch, or release messages can still arrive. The 180-second lease
TTL expires ownership; it is **not** a retention TTL. Do not age-prune these
records or apply an arbitrary TTL deletion policy.

Desktop hot maintenance uses indexed state/workspace/expiry lookups and
unfinished-only owner/recording queries rather than walking unbounded terminal
history. Stream recovery likewise selects unfinished intents, with indexed
owner/workspace associations. Retention therefore must not be confused with a
requirement to enumerate all historical records on every sweeper tick; this is
not a claim that every query is bounded independently of unfinished workload.

Monitor runner journal/state storage growth and free disk space; include the
SQLite stores and sidecars in consistent backups. Any administrative retention
maintenance requires a proven **permanent logical-identity/replay boundary**:
no executing or uncertain owner, no possible old message replay, and no reuse
of the retired identities. Guest stop alone is insufficient. There is no
routine age-based purge procedure here; do not blindly clear databases or
control directories while old messages can still be delivered.

Keep guest managed control directories intact:

- `/var/lib/opencuria/streams` for managed stdio process groups;
- `/workspace/.opencuria/managed-recordings` for managed recordings.

Do not run housekeeping that deletes these directories while a run is active or
cleanup is pending. Their identities, locks, and durable close tombstones fence
late launches. Playwright artifacts in `/workspace/.opencuria/playwright` are a
separate output directory, not the ownership store.

Managed execution uses a guest supervisor and **process-group** termination,
with boot ID, PID start time, session, and group identity checks to avoid killing
reused processes. Before launch, the expected physical guest token combines
`boot_id` with PID 1's `init_starttime`; the guest verifies it before publishing
process identity or executing the command. A delayed launch prepared before a
same-logical-instance stop/restart cannot silently refresh that token and run in
the new incarnation. Cleanup also checks the recorded physical incarnation. This is not cgroup containment. Ordinary descendants retaining
the group are covered; malicious or deliberately escaped descendants (for
example, a new session/process group) are excluded from the cleanup guarantee.
Never treat managed cleanup as a security boundary for hostile guest programs.

## Viewer ownership and authenticated proxy

Each browser tab has its own non-persisted `viewer_client_id`; surfaces within a
tab share/refcount its intent. A monotonically increasing `intent_revision`
fences delayed starts, renewals, and releases. A globally active desktop does not
prove that this tab owns a lease. Closing the last retained surface releases only
that tab's viewer intent, not other viewers or agent owners. That release waits a
1.5-second grace period so a surface remounted by navigation keeps the intent; an
explicit stop releases immediately.

The current REST interfaces under `/api/v1/workspaces/{workspace_id}` are:

- `POST /desktop/`: start, returning `202` with `task_id`;
- `POST /desktop/stop/`: release, returning `202` with `task_id`;
- `POST /desktop/renew/`: renew an existing held intent, returning `200`;
- `GET /desktop/status/?viewer_client_id=<uuid>`: query this tab's status.

The three POSTs require a body such as:

```json
{"viewer_client_id": "61d4857e-243a-442a-977f-75e01f156083", "intent_revision": 1}
```

These are authenticated, organization-scoped workspace operations. API keys need
`terminal:access`; requests use `X-Organization-Id`. A client ID is an association,
not authorization. Use the backend-issued viewer URL instead of exposing guest
KasmVNC directly. The authenticated `/ws/desktop/{workspace_id}/` proxy carries
the viewer association and renews the existing intent every 45 seconds during an
associated tunnel. An observer without an association does not acquire ownership.
Proxy renewal failure closes that tunnel; the proxy never acquires or releases a
lease to compensate. The webapp also renews retained intents every 45 seconds.

### Startup and viewer recovery

A `202` start response acknowledges dispatch, not readiness. The webapp observes
its own intent every second until both the held lease and an active proxy URL
are confirmed. Startup is bounded to 30 seconds; individual start/renew/status
requests are abortable after 10 seconds. Transient transport errors only retry
observation, not ownership acquisition. An expired/released intent or changed
runner epoch permits a bounded acquisition with a newer revision.

The viewer uses trusted KasmVNC status messages and observes the loaded document's
`noVNC_connected` class, so a delayed or missing message cannot leave a connected
frame hidden. Disconnect recovery reconciles ownership before remounting the
viewer, with three automatic retries and an explicit Retry action after failure.
Hidden browser tabs pause viewer handshake/retry timers. Switching between the
sidebar and modal preserves the same iframe and VNC connection.

Desktop process events and heartbeats carry a runner epoch and monotonically
increasing process generation. Late stops cannot delete a newer proxy route;
stopped-generation tombstones reject delayed active announcements. A heartbeat
probe failure is distinct from a confirmed stop. The desktop manager confirms
negative probes under its lifecycle lock before pruning a session.

Backend, runner, and webapp changes must be deployed together.

## Coordinated rollout

Plan a maintenance window; this is **not a zero-downtime upgrade**.

1. Stop admitting new runs and drain active runs, MCP connections, computer-use
   attempts, and viewer sessions. Resolve pending cleanup while guests are still
   reachable. Do not restart only one component during active ownership.
2. Back up the backend database and each runner's persistent state directory with
   its workspace disks/volumes. With runners stopped, copy the entire state
   directory so SQLite databases and any WAL/sidecar files are consistent. The
   managed ownership stores intentionally persist no secrets; do not add tokens
   or rendered environment to recovery notes/backups of those stores.
3. Deploy matching backend, runner, and webapp revisions together. Preserve runner
   identity, state paths, volumes, and guest control records.
4. For the original source setup, apply migrations before starting ASGI:

   ```bash
   cd backend
   .venv/bin/python manage.py migrate --noinput
   ```

   This is the same command documented in the root README's source setup.
   The Docker backend entrypoint runs migrations automatically. Keep the existing
   single-worker ASGI and independent lifecycle recovery worker setup; this
   feature does not replace lifecycle recovery.
5. Audit customized plugins for manual adoption, then restart backend, runner,
   and webapp as one coordinated release. Check runner connectivity and recovery
   before admitting new work. Old-epoch runs are aborted, not resumed.
6. Verify a new Playwright run: discovery alone should not activate a display;
   the first browser tool should activate it. Open two viewer tabs and confirm
   closing one does not release the other's intent. Check release/expiry cleanup
   and pending closing owners before returning to normal operation.

Do not restore or remove a single lease database as a shortcut: mismatched stream,
lease, journal, and guest state can destroy cleanup evidence. Rollback likewise
requires a coordinated drained deployment and consistent backups, not mixed
protocol versions or a promise of transparent resumption.
