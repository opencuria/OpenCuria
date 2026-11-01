# @opencuria/computer-use

Independent, run-scoped Computer Use MCP runtime for workspace Linux desktops.
The backend controls plugin exposure and lifecycle; this package has no provider
credentials or model loop.

Linux observations use AT-SPI for existing windows as well as agent-launched
apps; no browser debugging port is required. The runner enables accessibility
before starting the desktop and refreshes its session launcher for older images.
The native driver also enables the bridge when joining an existing session.

Applications that implement no accessibility interface (for example, a custom
canvas) still require screenshots and window-relative input. A window started
with accessibility disabled may need reopening once after an upgrade; the
driver reports this limitation instead of inventing actionable elements.

Verify real Chrome (manual and agent launch), GTK and Qt interactions on a
running workspace desktop:

```sh
npm run build -w @opencuria/computer-use
OPENCURIA_CUA_DESKTOP_TEST=1 CUA_REFERENCE_ROOT=/nonexistent node --test --test-concurrency=1 packages/computer-use/test/{accessibility,browser,desktop}.test.mjs
```

The Qt test needs `python3-pyqt5`; the GTK test needs `gir1.2-gtk-3.0`.
`CUA_REFERENCE_ROOT` above omits the optional proprietary reference comparison;
all functional desktop checks still run. To verify installed package bytes, set
`CUA_TEST_NODE` and `CUA_TEST_SERVER` to the installed Node and `server.js` paths
when running `accessibility.test.mjs`.
