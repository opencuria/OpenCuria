# Harness run ownership

`HarnessService` is the application facade and composition root. It binds
explicit port bundles rather than passing itself into execution components.

- `RunPreparation` validates engine ownership, provider-account selections,
  skills and ancestry and returns a `PreparedRun` value.
- `RunAdmission` owns the entire asynchronous admission, including preparation,
  before its first await. It rejects duplicate local starts, reserves persistent
  admission, creates the message shells, and transfers ownership to execution.
  Failed startup owns its rollback; a disconnected caller cannot erase a run
  that was already transferred to execution.
- `RunExecution` owns native execution and runtime teardown. External execution
  receives `ExternalExecutionPorts` plus the current context. It cannot reach
  the facade's internals. Engine authentication preparation takes only the two
  authorization callbacks it actually needs.
- `RunCancellation` owns ancestry, descendant closure, stop intent, cancellation
  and joining. Finalization is a cancellation fence, not another cancellable run.
- `ProviderTurnLifecycle` owns shared answer holders and provider revocation.
- `SubagentCoordinator` owns delegation, child admission and result coordination.

`RunRegistry` stores one `ActiveRun` per session, with three local phases:
`ADMITTING`, `RUNNING`, `FINALIZING`. A released run has no registry record.
Admission and execution do not retain competing owners. Stop intent, event lock,
ancestry and timeline context belong to that same record. Timeline dictionaries
contain evolving engine/presentation data, not task ownership or finalization
flags. Components and test fixtures acquire, transfer and release ownership
through named registry transitions and inspect typed records. There are no
parallel mutable dictionaries for individual run fields.

Task completion callbacks retain the admitted record's identity. They must not
look up a new record after finalization has removed the old one: doing so could
mistake an already executed cancellation for a task cancelled before entry and
run terminal persistence twice. A done task remains an admission fence until its
callback and shell cleanup explicitly release ownership. Reservation cancellation
joins the database thread and rolls back a successful reservation before releasing
local ownership. Descendant ancestry and explicit stop intent
remain until the abort tree has settled.

Persistent admission and external engine cleanup fences remain repository-owned.
These local task records do not replace database ownership or recovery checks.
