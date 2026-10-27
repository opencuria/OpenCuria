# Computer Use

Computer Use replaces the Agent-S planner with a product-owned workspace MCP
plugin. Activate **Computer Use** in the organization and select it for a
workspace. Build/general runs can use its tools directly; `task` can delegate a
desktop task to the optional `computeruse` specialist. Its existing model and
effort settings continue to apply. No separate grounding model is used.

## Architecture

The backend owns models, the ordinary tool loop, permissions, plugin selection,
run identity, desktop leases and recording policy. The runner remains an I/O
service: it installs a pinned artifact and owns VM processes, desktop input
coordination and recordings. The workspace runs the stdio MCP server, a
persistent JavaScript worker and a bounded Linux native I/O helper.

The public tools are `mcp_computer-use_cua_repl_js` and
`mcp_computer-use_cua_repl_js_reset`. `turn_ended` is a private lifecycle RPC,
filtered from model schemas and rejected by the agent invocation path. It
bypasses the execution lock so stopping a busy call cannot deadlock teardown.
All three harness engines use the same plugin tool contracts. Plan mode asks
before execution; explore denies these tools. Existing explicit denies win.
JavaScript is executable workspace code. The workspace VM is the isolation
boundary; the JavaScript worker is not a security sandbox.

The API shape follows the reviewed ChatGPT Computer Use design: persistent JS,
`cua` observations/actions, documentation at the first entry point, explicit
image emission and reset/lifecycle tools. The implementation is independent
OpenCuria code and does not install or depend on ChatGPT software. The initial
native adapter supports Linux X11 desktops and AT-SPI. Browser DOM automation
continues to use the browser plugin.

## Runtime contract

Start with `await cua.getState()` or `await cua.listWindows()`. Read the returned
documentation. Bind an observed window with
`var app = await cua.getApp({windowId: observedId})`. Prefer its fresh AX tree;
when the source is `x11`, it contains only limited window metadata. Activate a
window explicitly before keyboard input or coordinate actions. Coordinates
refer to the most recent window screenshot, including image scaling. They are
invalid after an input action, movement, resize, rebinding or desktop handover.
Element references are bound to an AX snapshot and checked for changed identity.

State lasts for one backend run, not across user turns or reconnects. Reset
clears JS bindings and native references while leaving applications open.
Timeout/cancellation kills the worker, releases helper input and fences late
callbacks; desktop generation or automation-owner changes reset state too.
Calls are never automatically replayed when side effects are uncertain.

Limits: 64 KiB code, 64 KiB text, two images totaling 4 MiB, 6 MiB result,
1–60,000 ms per JS call (default 30,000), 100 JS calls per run, 256 MiB worker
heap, 1,500 AX nodes and 32 levels. Every CUA operation must be awaited. A
separate supervisor owns deadlines so a synchronous infinite loop cannot block
cancellation. AT-SPI calls have their own native timeouts.

A durable per-workspace input owner serializes Computer Use with managed headed
Playwright calls. Another automation gets a busy error. Viewer leases remain
independent. An interrupted owner's input ownership is removed only after its
owned streams have been closed. Human input does not share this automation
lock; reobserve after intervening manual interaction.

## Settings, migration and recording

`GET/PUT /api/v1/computer-use-config/` exposes partial updates of `max_steps`
(1–1,000, specialist only), `screenshot_max_dimension` (320–7,680) and
`enable_recording` (default false). Unknown/obsolete fields are rejected.
`/agent-s-config/` returns 410 with the replacement endpoint. The migration
renames the org configuration, retains these useful values, removes Agent-S
fields and activates the new plugin for workspaces with prior computer-use use
or org configuration. Other workspaces opt in through plugin selection.
Historical Agent-S timeline rendering remains supported.

Recording starts on the first operational desktop call. Confirmed MP4 paths
are appended to the chat on success, error or interruption, after recording
finalization. Recording and viewer ownership use the existing runner resource
ledger; Computer Use never shuts down a desktop still held by a viewer.

## Packaging and rollout

Restart the backend and runner after deploying, then run Django migrations.
New desktop images include distribution packages for Xlib, Pillow, structlog,
PyGObject, AT-SPI and xclip. Older apt-based workspaces install these missing
packages during first provisioning. Network access to the distribution mirrors
and nodejs.org is required on a cold cache. Linux glibc x64 and arm64 are
supported; unsupported platforms fail with a health error.

`ensure_runtime_artifact("computer-use", "0.1.0")` uses the shared artifact
manager. The runner ships a deterministic bundle and downloads Node 22.23.2
from fixed official URLs with size/SHA-256 pins. It checks each installed file,
refuses changed or symlinked trees and publishes under a guest lock with atomic
no-replace rename. Existing Claude/Codex artifact pins remain unchanged.
The entry point is
`/opt/opencuria/runtimes/computer-use/0.1.0/opencuria-cua`.

From the repository root:

```sh
npm ci
npm run build -w packages/computer-use
python3 packages/computer-use/pack.py
npm run type-check -w packages/computer-use
npm test -w packages/computer-use
```

Commit the generated `runner/src/runtime/assets/computer-use-0.1.0.*` with the
source. Increment the package/plugin/artifact version and provide a migration
when changing a released runtime; immutable installed trees are never silently
replaced. The bundle carries the MCP SDK/transitive license notices.

Real desktop contracts require a private X11 display, a session D-Bus and GTK:

```sh
DISPLAY=:196 dbus-run-session env OPENCURIA_CUA_DESKTOP_TEST=1 \
  npm test -w packages/computer-use
cd runner
CUA_TEST_NODE_ARCHIVE=/path/to/node-v22.23.2-linux-x64.tar.xz \
  .venv/bin/pytest tests/test_computer_use_artifact.py -q
```

The GTK fixture is isolated, verifies actual saved UI data, exercises Unicode
semantic insertion and clipboard paste, checks screenshots/stale bindings and
cleans up after itself. Live model evaluations must identify the exact provider
model, inspect actual UI results independently and keep secrets out of reports.
