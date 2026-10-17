# Managed update verification

This report records workspace-local verification for the production-update branch.
It is not a certification of arbitrary infrastructure failures.

## Real processes and UI

An isolated PostgreSQL database, Redis namespace, stable Socket.IO gateway,
HTTP API releases `rollout-v1` / `rollout-v2`, separate harness worker processes,
a deterministic local OpenAI-compatible provider, and a real Docker workspace
were started. No external LLM or production service was used.

Verified:

- Version 1 run waited on a persisted user question. The answer was submitted
  through the version 2 API; version 1 completed its original run, then drained.
  A new root was executed by the version 2 worker.
- A real shell tool completed through worker → Redis broker → stable gateway →
  supervised runner → Docker → reply back to the owning worker. Its timeline and
  final answer contained `retained-shell-output`.
- The terminal kept shell PID **387**, its PTY and exported marker `preserved`
  across activation of a second runner source generation and terminal reattach.
  The live browser rendered the retained transcript. A real Docker HTTP idle
  timeout bug and an unawaited Socket.IO callback were found and fixed by this
  check rather than hidden by mocks.
- An actual isolated worker process was SIGKILLed during a delayed provider
  response. Its run was not replayed or adopted; recovery refused to release a
  native run without explicit runner-quiescence confirmation.
- Separate scheduler and lifecycle dispatcher processes acquired real PostgreSQL
  advisory locks. Their CLI readiness probes succeeded; version 2 deployment
  readiness also succeeded against the retained stable operations release.
- A separate real Compose stack started PostgreSQL 16, Redis, Nginx, webapp,
  API, stable Socket.IO gateway, harness worker, scheduler and recovery in actual
  containers. All Django migrations ran in its own database; HTTP health through
  Nginx and the full worker/schema/scheduler/recovery CLI readiness passed.
  This acceptance stack mounted the current backend source read-only into the
  locally built image; it did not publish images or modify the normal dev stack.

The actual UI artifact is at
`/workspace/screenshots/production-updates/terminal-retained.png`.
Workspace logs and terminal/run observations are stored alongside it; private
local setup environment and test passwords are not part of the repository.

## Project checks

The implementation commit hook completed with `Ready to commit.`: **2963 backend
passed / 32 skipped**, **817 runner passed / 2 skipped** plus 28 subtests,
**1943 webapp tests** and TypeScript checking. The implementation was committed
and pushed as `57f4f24` on `feat/production-safe-updates`. PostgreSQL-only suites
(**27 passed** in the final integrated run) and the opt-in live Docker test were
run separately (not counted as pretending skipped tests passed). PostgreSQL test
teardown reported retained test connections in some runs; assertion results were
successful, but the warning is recorded rather than hidden.
Production webapp build and targeted backend/runner/frontend lint also passed.
Existing deprecation/test-fixture warnings remain; they are not hidden.

## Automated real Docker regression

`runner/tests/test_supervised_live.py` starts the actual supervisor and two real
worker child processes against an explicitly selected isolated Docker daemon.
It tests capability readiness, activation of a distinct immutable code snapshot,
unchanged runner Socket.IO identity, old-worker draining, same shell PID and
environment on reattach, actual stdin ACK, and a deliberately duplicated input
whose guest-side file has exactly one line. It passed locally. An additional
actual-process check also issued a journaled `task:stop_workspace` after the
session drained and verified the durable `workspace:stopped` result plus backend
result ACK; the real workspace container was stopped successfully.

The test is opt-in because it creates/removes its own Docker container. It must
never target a production daemon. Regular suites skip it when the explicit
`OPENCURIA_RUNNER_LIVE_DOCKER_URL` is absent.

## Database and failure boundaries

Real PostgreSQL tests cover concurrent gateway admission (two actual database
connections, exactly one turn/outbox), exclusive migration advisory lock,
rejecting a target schema unsupported by an old retained release before DDL,
and refusing the fresh-DB bootstrap flag on an existing database.

Deterministic tests additionally cover:

- unique process registrations, sticky drain and pre-drain committed starts;
- same-version ownership, wrong tokens/workspaces/roots/runner IDs;
- real Redis subprocess reply routing to another worker process;
- cross-process gate resolution and pinned completion;
- critical tool-start persistence failure: no tool executes, ownership retained;
- uncertain runner timeout: no next tool/provider step, retained closing state;
- user abort during a real accessor RPC wait: local waiter cancelled, but no
  premature ownership release before remote execution quiescence is proven;
- PTY replay cursor gaps/overflow and preservation of live PTY;
- input duplicate suppression and failed input not acknowledged as successful;
- source tamper detection, stale authority rejection, distinct backend-worker
  and runner-generation IDs, and desktop resource affinity without repinning
  a new agent's entire run;
- CLI/shell failed-readiness behavior retains old routing and never stops workers;
- existing authentication, permission, runtime, scheduling, chat and file suites.

## Deliberate limits / remaining release acceptance

- The public deployment shell was validated through Compose config generation,
  failure-path shell tests and a real equivalent separated Compose stack. A
  registry pull/cutover using **published** immutable release images is still a
  deployment-environment acceptance step; no images were pushed by this work.
- Docker source-generation/PTY and native harness flows were exercised live.
  A complete live Claude SDK + QEMU + MCP + desktop recording **mixed-release**
  acceptance matrix was not performed. Their existing suites and routing tests
  are necessary but not equivalent to that matrix. Do not describe this report
  as proving every engine/runtime combination production-ready.
- Stable Socket.IO gateway/supervisor/interpreter/dependency changes are declared
  maintenance operations. Arbitrary stable-plane crashes, host failures and
  PostgreSQL/Redis failovers are not guaranteed to preserve active LLM/byte streams.
- Replay retention is bounded. Native crash quiescence is an explicit operator
  attestation; a heartbeat timeout is intentionally not treated as proof.
- Destructive contract schema changes remain maintenance-only. Online allowlist
  approval is deliberately conservative and does not auto-migrate arbitrary DDL.

Run the complete commit gate after integration, and reproduce relevant live tests
with the exact production image/runtime combination before enabling managed mode.
Legacy local startup remains the default until an operator opts in.
