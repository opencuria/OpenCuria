# Runner application services

`RunnerService` is the composition root: it wires the domain and infrastructure
services once per process and exposes them as attributes. Callers use the owning
service directly, e.g. `get_runner_service().workspace_lifecycle.stop_workspace(...)`.

- Every service takes its repositories and collaborators as keyword-only
  constructor arguments. None receives `RunnerService` or an untyped shared context.
- Domain services (workspace, runner, image, interactive, Git, files) depend on
  `ProcessManagerService`, `CredentialSyncService` and infrastructure (transport,
  ownership, frontend bus, task dispatch), which depend on repository adapters.
- No mutable state lives on a service class; each `RunnerService` gets its own.

| State | Owner |
| --- | --- |
| Pending process and Git RPCs | `RpcRegistryService` |
| Git serialization locks | `GitOperationsService` |
| Credential deliveries awaiting reconciliation | `CredentialSyncService` |
| Process verification work from heartbeats | `HeartbeatReconcilerService` |
| Terminal routes, desktop observations, epochs, tombstones | `InteractiveSessionState` |

Test a service by constructing it with fakes, e.g. `ProcessManagerService` with a
fake transport and a real `RpcRegistryService`; patch the owning service, not the root.
