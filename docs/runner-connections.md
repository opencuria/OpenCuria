# Runner connection lifecycle

OpenCuria runs one runner application under a connection supervisor. The runner
uses Socket.IO as a transport, with Socket.IO's own automatic reconnection
disabled. Each connection attempt creates a fresh client so a failed attempt
cannot leave handlers, tasks, or transport state attached to the next one.

## Retry and failure policy

Transient network, backend availability, and timeout failures are retried
indefinitely with exponential backoff from 2 seconds to 30 seconds and jitter.
The delay bounds are configurable with `RUNNER_RECONNECT_DELAY` and
`RUNNER_RECONNECT_DELAY_MAX`. Invalid configuration and explicit authentication
or authorization failures are fatal: log the cause and exit nonzero instead of
retrying an unusable credential/configuration.

Initial timeout defaults are 10 seconds for connect, registration, and
heartbeat operations; 120 seconds for runtime preparation; and 5 seconds for
cleanup. Configure these with `RUNNER_CONNECTION_TIMEOUT`,
`RUNNER_RUNTIME_SETUP_TIMEOUT`, and `RUNNER_CLEANUP_TIMEOUT`. The heartbeat
interval defaults to 15 seconds and is configurable with
`RUNNER_HEARTBEAT_INTERVAL`.

The runner/backend protocol is explicitly versioned as `protocol_version: 1`.
Both ends must require a matching version; there is no legacy or implicit
fallback. An acknowledgement confirms persisted runner presence only. It does
not mean workspace jobs have completed.

## What disconnecting closes

A connection loss closes interactive session PTYs, generic streams, and desktop
proxy tunnels. In-flight runtime mutations finish before a replacement session is
installed; their drain is bounded by `RUNNER_RUNTIME_SETUP_TIMEOUT`. If cleanup
cannot finish safely within its limits, the runner exits nonzero rather than
allowing old work to overlap a new session. Workspaces (including running VMs)
and persistent user processes are left running; reconnecting does not reset or recreate them. The SSH
reachability health-check loop has the runner application's lifetime and
continues across backend disconnects, so it can still check and self-heal
unreachable QEMU workspaces.

Workspace status snapshots and runner liveness are separate signals. Snapshots
report runtime state; periodic heartbeats establish liveness independently.
On registration the backend reports existing workspaces as online, and a
current runner disconnect reports them offline. The frontend applies these
workspace-scoped events immediately, without waiting for its periodic list
refresh.

## Operational boundaries

The current topology has a single backend and provides no distributed lease or
reset guarantee. Do not run multiple competing runner processes for the same
runner identity. The service manager policy is unchanged: `systemd`
`Restart=always` and Compose `unless-stopped` can restart a process even after a
fatal nonzero exit. Configure service-level restart behavior separately if
fatal exits must remain stopped for operator intervention.
