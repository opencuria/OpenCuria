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
