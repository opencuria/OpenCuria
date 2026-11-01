# Runtime architecture refactor verification

Date: 2026-11-01. Work is on `refactor/explicit-runtime-architecture`.

## Implemented boundaries

1. Harness admission, preparation, execution, cancellation, provider scopes and
   subagents have explicit collaborators. `RunRegistry` stores one typed record
   per local run, with three phases and guarded ownership transitions. Completion
   retains record identity; committing writes are joined before cancellation can
   reach rollback. See [run ownership](harness-run-architecture.md).
2. Backend runner management composes 17 standalone services. Repositories,
   transport and domain collaborators are constructor dependencies. Interactive
   state and correlation registries are instance-owned. Compatibility forwarding
   methods retain existing adapter signatures without dynamic forwarding or mixin
   inheritance. See [service composition](../backend/apps/runners/services/README.md).
3. Runner transport dispatches explicit commands through persistent execution;
   services return terminal results. `RunnerApplication` composes journal,
   execution, recovery evidence and Socket.IO. No Socket.IO method/handler
   replacement installs execution behavior.
4. Web and mobile share timeline request ownership, snapshot/live reconciliation,
   detail hydration and cache retention in `ChatTimelineController`. Vue publishes
   into reactive rows; native clients preserve immutable snapshots. See
   [chat state ownership](../packages/chat-core/README.md).
5. HTTP/realtime adapters submit encrypted persistent commands. An independent
   execution process owns native tasks, gates, scheduler, notifications and
   recovery. Process composition in `config/execution.py` injects job lifecycle
   ports; the harness does not import its scheduler consumers. See
   [execution ownership](execution-architecture.md).

The execution queue has three states and separate bounded admission/control
lanes. Stop/delete fence both queued and already-claimed earlier starts for the
same session; later starts wait behind their control barrier. Settings snapshots
reject stale queued configuration. Terminal-result retries preserve the original
outcome despite cancellation. Shutdown drains core runs, metadata, provider holds
and callback-created cleanup without misreporting process shutdown as user stop.
Recovery reconciles only matching old assistant identities and external
projections, preserving new admissions and independent child runs.

## Verification results

| Check | Result |
| --- | --- |
| Complete backend suite, real pinned Codex runtime enabled | 5,274 passed; 28 PostgreSQL-only cases skipped (covered separately) |
| Complete runner suite | 1,468 passed; 36 subtests passed; no failures or skips |
| Web | 2,329 passed across 215 files |
| Shared chat core / API client | 119 / 58 passed |
| Mobile state logic / native UI | 166 / 129 passed |
| Computer-use contracts, isolated headed Chrome and GTK included | 11 passed, no skips |
| PostgreSQL locking/concurrency supplement | 45 runner-locking cases and 75 complementary cases passed |
| TypeScript workspace checks and web production build | Passed |
| Final run-settings submission/immutability regression selection | 10 passed |
| Python static error checks across changed files | Passed |
| Django system check, migration drift, shell syntax and Compose checks | Passed |

Specific regression selections also cover record-release callback gaps,
commit/cancellation barriers, frozen native snapshots, Vue row identity, stream
versus loaded timeline races, reply validation, bounded command saturation,
completion persistence failures, shutdown holds and all recovery entrypoints.

The native process integrations submit a real harness run from a separate API
process and through authenticated HTTP on actual Uvicorn. They stop/restart the API
while the provider is held, then confirm the worker completes the same persisted
assistant. The HTTP case uses real scheduler, notification, recovery and Redis
startup through the production process composition. Redis tests use a separate subscriber process,
large transient chunks, real harness and git waiters, invalid ownership and
stream disconnect/acknowledgement behavior. Real Claude and Codex CLI tests use
local provider fixtures, without paid inference or real provider credentials.

The backend development environment was brought to the Claude SDK version already
pinned in `backend/requirements.txt`. Legacy adapter tests now inject the execution
service explicitly or mock their owning runner component. Existing invalid-UUID
and recovery-message assertions were aligned with their unchanged storage/service
contracts; architecture-boundary tests were retained and passed.

## Reproduction

```bash
cd backend
OPENCURIA_TEST_CODEX_BINARY=/opt/opencuria/runtimes/codex-agent/0.166.0/bin/codex-app-server \
  .venv/bin/pytest -q -rs
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
cd ../runner
OPENCURIA_TEST_CODEX_PACKAGE=/tmp/opencuria-optional-artifacts/codex-package.tar.gz \
CUA_TEST_NODE_ARCHIVE=/tmp/opencuria-optional-artifacts/node.tar.xz \
CLAUDE_TEST_BINARY=/tmp/opencuria-optional-artifacts/claude \
  .venv/bin/pytest -q -rs
cd ..
npm run type-check
npm run test
npm run build -w webapp
```

Pinned-runtime paths can be overridden for the test installation. The optional
native desktop contracts require GTK accessibility, a display, and a shared
D-Bus session; run them serially in an isolated Xvfb session. PostgreSQL cases
were run against a disposable local cluster, which was stopped afterward. The
standard SQLite run reports PostgreSQL-only skips; the supplement exercises the
row-lock behavior with a real PostgreSQL server. See the final log skip summary
for remaining platform/fixture-specific conditions.

Workspace evidence:

- [Backend complete log](/workspace/backend-architecture-final.log)
- [Runner complete log](/workspace/runner-architecture-final.log)
- [Pinned artifacts and runner prerequisites](/workspace/test-results/runner-optional-environment.json)
- [Command ordering and real native-process tests](/workspace/execution-command-ordering-tests.log)
- [Architecture boundaries and execution integration](/workspace/final-architecture-boundaries.log)
- [Real HTTP/API-restart acceptance](/workspace/execution-http-acceptance-tests.log)
- [PostgreSQL complement](/workspace/test-results/postgres-complement.log)
- [Native desktop contracts](/workspace/computer-use-isolated-desktop.log)
- [Native desktop screenshot](/workspace/architecture-verification/desktop.png)

## Deployment boundary

Apply migrations `0045` through `0047` and run `python manage.py execute_harness`
alongside the API. Both Compose profiles and the source/systemd installer wire
this independent worker. During the first upgrade, stop the old combined backend
before starting the new execution owner.

The supported topology retains one execution worker per database and one realtime
API gateway. Terminal/desktop connections remain gateway-owned; arbitrary
replicated realtime gateways are not claimed by this change. API restarts no
longer own native agent tasks or scheduled/notification job lifetimes. PostgreSQL
execution contenders on different hosts require the documented shared lock path.

The pre-existing untracked `docs/pointing-capture-efficiency.md` was left untouched.
