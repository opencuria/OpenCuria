# @opencuria/computer-use

Chat-scoped Computer Use MCP runtime for workspace Linux desktops. The backend
keeps one runtime per chat (idle timeout `COMPUTER_USE_SESSION_IDLE_SECONDS`)
and controls plugin exposure and lifecycle; this package has no provider
credentials or model loop. JavaScript state survives turns (`turn_ended` with
`Stop` only fences the ended turn) and display restarts (windows must be bound
again).

The native driver (`native/linux.py`) reads AT-SPI directly over D-Bus with
pipelined requests, keeps an event-maintained mirror of each bound window's
tree (incremental for Chromium, GTK and Gecko; complete re-reads for toolkits
with incomplete events such as Qt) and waits after input until the application
stops reporting changes. Element IDs follow AT-SPI object identity, so they
stay stable while an element exists. No browser debugging port is required.
The runner enables accessibility before starting the desktop; the driver also
enables the bridge when joining an existing session.

The agent-facing API (`src/api.ts`, documented in `src/contract.json`) adds
queries with action-time resolution and auto-wait, `find`, `waitFor`, change
lists for repeated observations, `setValue` (text, options, checkboxes,
numbers), subtree observations, layout contexts for unlabeled controls,
`chooseFiles` for GTK, desktop-portal and Qt file dialogs, `dropFiles` over
XDND, scaled or cropped screenshots, and `nodeRepl.readFile`.

Applications that implement no accessibility interface (for example, a custom
canvas) still require screenshots and window-relative input. A window started
with accessibility disabled may need reopening once after an upgrade; the
driver reports this limitation instead of inventing actionable elements.

Unit tests (no desktop needed): `npm test -w @opencuria/computer-use`
(`test/fixtures/native_ax.py` covers the driver against an in-memory AT-SPI
bus). Verify real Chrome, GTK, Qt and Thunar interactions on a running
workspace desktop:

```sh
npm run build -w @opencuria/computer-use
OPENCURIA_CUA_DESKTOP_TEST=1 CUA_REFERENCE_ROOT=/nonexistent node --test --test-concurrency=1 packages/computer-use/test/*.test.mjs
```

The Qt tests need `python3-pyqt5`; the GTK tests need `gir1.2-gtk-3.0`.
`CUA_REFERENCE_ROOT` above omits the optional proprietary reference comparison;
all functional desktop checks still run. To verify installed package bytes, set
`CUA_TEST_NODE` and `CUA_TEST_SERVER` to the installed Node and `server.js` paths
when running `accessibility.test.mjs`. After source changes run `node build.mjs`
and `python3 pack.py`, which writes the runner asset
`runner/src/runtime/assets/computer-use-<version>.*`.
