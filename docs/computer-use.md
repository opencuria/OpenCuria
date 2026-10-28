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
All three harness engines use the same plugin tool contracts. Codex exposes
`js` and `js_reset` through a native managed MCP bridge with
`omit_tools_from = ["code_mode", "deferred"]`, including for models that require
Code Mode. Its private Unix socket relay forwards to the existing backend
dispatcher; approvals, events, desktop ownership and images stay on the ordinary
managed-tool path. No `functions.exec` wrapper or string image transport is needed.
Plan mode asks
before execution; explore denies these tools. Existing explicit denies win.
JavaScript is executable workspace code. The workspace VM is the isolation
boundary; the JavaScript worker is not a security sandbox.

The agent-facing contract is pinned to the local ChatGPT `@oai/cua-repl` 0.1.2
computer-only Linux surface in `packages/computer-use/src/contract.json`.
Descriptions, input schemas and first-use documentation match that reference.
The API follows its design: persistent JS,
`cua` observations/actions, documentation at the first entry point, explicit
image emission and reset/lifecycle tools. The implementation is independent
OpenCuria code and does not install or depend on ChatGPT software. The initial
native adapter supports Linux X11 desktops and AT-SPI. Browser DOM automation
continues to use the browser plugin.

## Runtime contract

Start with `await cua.getState()` or `await cua.listWindows()`. Read the returned
documentation. Bind an observed window with
`var app = await cua.getApp({windowId: observedId})`. Prefer its fresh AX tree;
when the source is `x11`, it contains only limited window metadata.
Linux input is delivered directly to the selected window;
it requires no activation and does not move the desktop pointer. Coordinates
are relative to that window. Input, inventory and observations share a 100 ms
post-action settling deadline. Reobserve after UI changes before choosing the
next target.
Element references are bound to an AX snapshot and checked for changed identity.

State lasts for one backend run, not across user turns or reconnects. Reset
clears JS bindings and native references while leaving applications open.
Timeout/cancellation kills the worker, releases helper input and fences late
callbacks; desktop generation or automation-owner changes reset state too.
Calls are never automatically replayed when side effects are uncertain.

Observations emit their own text and images. Inventory is JSON text, named text
channels are accumulated separately, and screenshots are MCP image blocks with
`codex/imageDetail: original`. `nodeRepl.emitImage` accepts PNG/JPEG/WebP bytes,
data/file URLs and `{ bytes, mimeType }`. Execution diagnostics remain private
MCP metadata. Empty output and reset responses follow the reference contract.
`cua.rewriteDocumentation()` repeats instructions without resetting JS bindings.

Workspace bounds: 64 KiB code, 64 KiB text, two images totaling 4 MiB, 6 MiB result,
positive JS timeouts (default 30,000 ms), 256 MiB worker
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
The migration renames the org configuration, retains these values, removes Agent-S
fields and activates the new plugin for workspaces with prior computer-use use
or org configuration. Other workspaces opt in through plugin selection.

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

`ensure_runtime_artifact("computer-use", "0.2.0")` uses the shared artifact
manager. The runner ships a deterministic bundle and downloads Node 22.23.2
from fixed official URLs with size/SHA-256 pins. It checks each installed file,
refuses changed or symlinked trees and publishes under a guest lock with atomic
no-replace rename. Existing Claude/Codex artifact pins remain unchanged.
The entry point is
`/opt/opencuria/runtimes/computer-use/0.2.0/opencuria-cua`.

From the repository root:

```sh
npm ci
npm run build -w packages/computer-use
python3 packages/computer-use/pack.py
npm run type-check -w packages/computer-use
npm test -w packages/computer-use
```

Commit the generated `runner/src/runtime/assets/computer-use-0.2.0.*` with the
source. Increment the package/plugin/artifact version and provide a migration
when changing a released runtime; immutable installed trees are never silently
replaced. The bundle carries the MCP SDK/transitive license notices.

Real desktop contracts require a private X11 display, a session D-Bus and GTK:

```sh
xvfb-run -a dbus-run-session env OPENCURIA_CUA_DESKTOP_TEST=1 \
  npm test -w packages/computer-use
cd runner
CUA_TEST_NODE_ARCHIVE=/path/to/node-v22.23.2-linux-x64.tar.xz \
  .venv/bin/pytest tests/test_computer_use_artifact.py -q
```

The GTK and Chrome fixtures are isolated, verify actual saved UI data and page
navigation, and exercise Unicode insertion, clipboard paste, native keys and
screenshots without activation or browser automation. Reference compatibility
tests compare identical inputs against the installed ChatGPT runtime when
available (`CUA_REFERENCE_ROOT` overrides its location). They compare tool
schemas, descriptions, text channels, image inputs, errors, reset and the app
facade; they do not require ChatGPT at deployment. Migration 0013 upgrades only
the built-in runtime and its tool exposure, preserving workspace activations
and custom permission settings. Live model evaluations must identify the exact provider
model, inspect actual UI results independently and keep secrets out of reports.
