# Claude Agent LIVE E2E (local-only, opt in)

`claude-agent-live.py` and `tests/claude-agent-live.spec.ts` exercise the actual
Django REST/session persistence + frontend Socket.IO + Vue chat, with the pinned
`claude-agent-sdk==0.2.164` and its bundled Claude Code CLI `2.1.292`. The local
Anthropic Messages fixture deterministically calls native **Agent**, serves the
SDK child session's native **Read** call, then invokes the registered OpenCuria
**question** tool and waits for the browser's real answer. The pinned CLI reports
its task-started agent type in its SDK event; the UI assertions check that actual
persisted event, not the requested subagent label.

Docker, QEMU, Redis, a native runner, real provider credentials, and remote
inference are not used. SQLite, OS files, runtime state and CLI home stay under
`/tmp/opencuria-claude-live` (override with `--state-dir`). A synthetic running
workspace/online runner is seeded into only that DB; a narrow WorkspaceAccessor
implements the runner/runtime boundary. It refuses workspace shell commands or
mutations, returns only synthetic `/workspace` files, and launches only the
pinned SDK CLI. The CLI gets a dummy token and local API base URL in an isolated
Linux mount+network namespace: the host `/workspace` is overmounted with a
private fixture-only tmpfs tree, and a new network namespace has no external
route/interface, only a veth to the local SSE stub/deny proxy. The backend's
outbound proxy refuses any unexpected HTTP(S) request. Thus the script is safe
from deploying a real key or accidentally reaching provider inference.

## Prerequisites

- Linux with root/CAP_SYS_ADMIN, `ip`, and `iptables` (network namespace setup
  is fail-closed; it does not change existing namespaces or default routes).
- `backend/.venv` with current migrations and pinned `claude_agent_sdk`/aiohttp;
  `webapp/node_modules`, `e2e/node_modules`, and Playwright Chromium installed.
- Local backend ports 18081–18083 and web port 5177 available.
- A **separately started** frontend Vite process using
  `vite.claude-agent-live.config.mjs`; this script does not start or kill other
  developer app processes.

## Run

From `/workspace/OpenCuria` (after reading `AGENTS.md`):

```bash
backend/.venv/bin/python e2e/integration/claude-agent-live.py serve --reset
# Separate terminal/process, keep the dedicated proxy config:
cd webapp && npm run dev -- --host 127.0.0.1 --config vite.claude-agent-live.config.mjs
# Browser E2E; set opt-in explicitly:
cd e2e && E2E_CLAUDE_AGENT_LIVE=1 npx playwright test -c claude-agent-live.config.ts
```

URLs: Vite `http://127.0.0.1:5177`; backend `http://127.0.0.1:18081`.
Fixture login is `claude-live@localhost.test` /
`OpenCuria-claude-live-2026!`; it exists only in the disposable SQLite DB. The
browser enters through the regular login form and reads the seeded user-owned
Claude connection, not a mocked connection endpoint. The screenshot is written
to `/workspace/.opencuria/playwright/claude-agent-live-final.png`.

The API fixture handles `SIGTERM` by gracefully shutting down its local
servers, killing its isolated CLI process group, removing only its veth rules,
and reaping the CLI namespace. Afterward, remove only the marked fixture DB/files:

```bash
backend/.venv/bin/python e2e/integration/claude-agent-live.py cleanup
```

`--reset`/`cleanup` refuse non-fixture directories; both require their owner
marker once a migration/seed was completed. Do not point the harness at the app's
development DB or at any existing workspace.

The Messages stub honors the request's `stream` field: streaming requests receive
Anthropic SSE, and `stream: false` receives a standard Anthropic JSON Message. It
records only request metadata (streaming flag, model, advertised tool names and
whether the child-agent header is present), never prompt text or credentials.

## Coverage / limitations

The LIVE browser flow also exercises the real provider-settings UI switching
between synthetic API-token and subscription-token modes, verifies safe REST
responses and blank secret inputs, and selects Claude Plan then Build before
sending. These fixture-only tokens never reach remote inference.

The SSE provider and WorkspaceAccessor/runner RPC boundary are synthetic; the
pinned native CLI, SDK protocol, backend engine/session/question persistence,
REST authentication, frontend websocket fan-out and browser UI are real. There
is no real Docker container or guest VM. The frontend model dropdown's generic
(non-Claude) provider catalog is supplied as local fixture metadata; Claude model
catalog and owned encrypted Claude connection are exercised through production
REST services. Do not report this as remote provider, runner, Docker, QEMU or
production-network coverage.
