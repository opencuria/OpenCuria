# Agent desktop claim verification — 2026-10-31

The backend exposes `desktop(start|stop|status)` through the active Computer Use
plugin. Desktop-dependent MCP servers within an agent execution share one
activation lease. Parallel runs, native child executions, and viewer tabs keep
independent ownership.

## Automated checks

The relevant Codex/MCP/backend selection passed 307 tests, the Claude selection
passed 405, and the runner desktop/managed-process selection passed 225 tests
plus five subtests. Subsequent focused checks cover the additional native-child
MCP, recording acknowledgement-loss, real SDK scope ownership, and delayed-input
regressions. These are workspace-local tests with scripted provider endpoints;
no live inference or production services are involved.

Coverage includes:

- A viewer holding the display while the current agent has no claim; `status`
  reports `own_claim=false`, `held_by_others=true`, `needs_start=true`.
- Canonical plugin policy migration, preserving customized exposure allowlists.
- Independent parallel agent claims, repeated start/stop, automatic CUA and
  headed Playwright activation, and release on run/child completion.
- Native Codex/Claude child attribution and isolated MCP lifetime scopes;
  entered SDK scopes are closed by the task that owns them.
- Durable activation ordering, stale holds/stops, ended-owner tombstones,
  runner epoch changes, expired claims, and unconfirmed physical stops.
- Input ownership contention, cancelled calls, uncertain acquisition/release,
  and delayed input messages from an earlier operation of the same agent.
- Recording segments across stop/start and lost start acknowledgements without
  fabricated video references.
- Real guest supervisor subprocesses preserving the display pair for Codex and
  Claude profiles. A process starts before Xvfb; its later Bash child connects
  with `xdpyinfo` after Xvfb starts, without changing the parent environment.
- The pinned Codex app-server and Claude SDK/CLI executing native shell commands
  that observe `DISPLAY=:1` and `XAUTHORITY=/root/.Xauthority`.
- A pinned Codex native child invoking the managed `desktop` MCP surface with
  its own observed thread/turn scope, including replay rejection.

Relevant suites:

```sh
cd backend
OPENCURIA_TEST_CODEX_BINARY=/path/to/verified/codex-app-server .venv/bin/pytest -q \
  apps/harness/tests/test_codex*.py apps/harness/tests/test_agent_desktop.py \
  apps/harness/tests/test_mcp*.py apps/harness/tests/test_desktop_leases.py \
  apps/harness/tests/test_computer_use_mcp.py \
  apps/harness/tests/test_computeruse_tools.py
.venv/bin/pytest -q apps/harness/tests/test_claude*.py
cd ../runner
.venv/bin/pytest -q tests/test_desktop*.py tests/test_managed_streams.py \
  tests/test_managed_desktop_wiring.py tests/test_websocket_interface.py \
  tests/test_service_desktop.py
```

## Headed browser smoke

A separate local smoke used the real MCP SDK, `@playwright/mcp@0.0.84`, headed
Google Chrome, the production `McpRuntime` and `AgentDesktop`, and the actual
runner lease store/manager. Only the VM transport and display process launch
were substituted: local subprocesses, Xvfb on an unused `:1`, and a local viewer
readiness port. The runner's X11 handshake verification remained real.

Discovery left the desktop inactive. In each of two cycles, `browser_navigate`
started the display, rendered a uniquely titled page and returned its snapshot.
`desktop(stop)` released activation and stopped the display while retaining the
same MCP connection. The second navigation recovered a fresh headed Chrome
session. Both cycles used one agent lease. Final cleanup closed the MCP server
and the test-owned display/listener.

This verifies headed browser recovery across display stop/start, not full QEMU
provisioning or the webapp's KasmVNC viewer. Backend and runner must be deployed
together; desktop protocol 2 and the existing drained rollout apply.
