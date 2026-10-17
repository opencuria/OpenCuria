# Runner

The runner executes Docker/QEMU workspaces and exposes lifecycle, execution,
files, terminal, desktop, and git interfaces. Agent and plugin policy belongs to
the backend. See the root [distributed setup](../README.md#-distributed-setup)
for Docker and native QEMU installation, or [source setup](../README.md)
for local development.

## Operations

- [Managed desktop resources](../docs/managed-desktop-resources.md): generic
  leases, persistent SQLite ownership stores, guest control directories,
  process-group cleanup limits, epoch recovery, and coordinated rollout.
- [Lifecycle recovery](../docs/image-lifecycle-implementation.md#recovery-hardening-operator-workflow).

## Versioned code generation updates (opt-in)

Normal `serve` stays on the legacy in-process runner unless
`RUNNER_SUPERVISED_MODE=true` is explicitly enabled. Supervised mode keeps one
stable backend Socket.IO identity and one exclusive operation Journal in the
supervisor, while each handler/service generation runs from a separate immutable
source snapshot in a child Python process. The generation bridge uses a private
mode-0600 Unix socket, same-UID peer credentials, a random per-supervisor secret,
bounded length-prefixed IPC and explicit request/ACK replies. Workers never open
the Journal and never connect to the backend.

```sh
# Initial rollout is a scheduled restart; default config remains legacy.
RUNNER_SUPERVISED_MODE=true opencuria-runner serve
# Stage source from a local runner checkout (never downloaded by a network RPC).
opencuria-runner stage-generation /srv/runner-src release-2026-10-16 --stage-only
opencuria-runner stage-generation /srv/runner-src release-2026-10-16
opencuria-runner upgrade-status
```

The activation marker is local/admin-owned and applied only after the new worker
connects to IPC and reports the required real handler capability manifest. Existing
terminal IDs, stream connection IDs, desktop tunnel IDs, process IDs, lifecycle
operations and in-flight request IDs remain pinned to their original generation.
Workspace lifecycle/maintenance is serialized by the supervisor; per-generation
caches are not shared. Old generations are retained while session ownership or
active callbacks remain, without a fixed kill timeout. Draining state and owned
session counts are available from `upgrade-status` and the heartbeat metadata.
The desktop epoch and durable SQLite lease stores stay owned by the stable
supervisor across generation changes.

A worker that loses IPC after a command may have completed a guest-side effect;
that request is never automatically replayed on another generation. Its result is
unknown until existing journal/runtime evidence resolves it. Stream output uses
supervisor `sio.call` and returns its backend ACK before the worker advances, so
existing output backpressure remains end-to-end. A fully drained old generation
can be retired without disconnecting backend or sessions in the active generation.

The stable supervisor itself is intentionally not hot-updated. Updating it still
requires the ordinary maintenance restart; do not force-stop it while status
reports owned sessions. A deployment runner image must include the same Python
runtime dependencies/source environment as the worker generations; the local
snapshot contains source only and Python imports shared installed dependencies.

## QEMU SSH resilience

Open process/terminal channels share a cached SSH transport. Acquiring it never
executes a liveness command: a slow guest or a refused session must not disconnect
unrelated agent streams. Protocol keepalives run after 30 seconds of inactivity
and tolerate five unanswered requests (about three minutes without incoming
traffic before failure detection; not a hard deadline under continuing traffic).

Guest health checks use one disposable, separately authenticated SSH connection
per instance, with the same pinned host-key policy. Their 15-second deadline and
bounded transport cleanup also cover a stalled session open without leaking
channels or closing the agent transport. Health observations never restart a VM.

Explicit stop/restart/remove invalidates connection ownership before cleanup;
late connections cannot repopulate the cache. Small per-instance generation/lock
tombstones remain stable for queued callers and same-ID recreation. Genuine
transport loss still ends existing streams: new calls reconnect, but issued
commands and agent runs are **never automatically replayed**. A missing remote
exit status is unknown, not exit 0; integer-only exec/build paths fail explicitly.

Requires AsyncSSH 2.15+ (the public connection-closed API). After updating runner
code/dependencies, restart the runner when no agent run is active; restarting
cannot migrate existing SSH streams. No guest/image changes are required.
