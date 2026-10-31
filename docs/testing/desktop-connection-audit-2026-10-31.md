# Desktop connection audit — 2026-10-31

## Outcome and scope

The investigation found reproducible failures in desktop tunnel ownership,
HTTP response handling, and browser recovery. The fixes cover these paths and
add regression tests. Real KasmVNC 1.3.3 connections now pass the fault-injection
browser checks in Chromium and WebKit.

The supplied browser log does not identify one definitive cause of the original
incident. WebSocket code `1006` means that the browser did not receive a normal
close frame; it does not distinguish a network interruption, failed upstream
handshake, or proxy teardown. The fixes address independently reproduced defects
that can make these failures persistent or prevent reliable recovery.

## Connection flow

1. **Viewer intent.** `useDesktopSession` retains local surface owners through
   `useDesktopSessionCoordinator`. A tab has its own viewer client UUID and
   increasing intent revision. A start POST acknowledges task dispatch, not a
   connected viewer. The coordinator polls the authenticated client's lease
   every second until both `held` and an active proxy URL are confirmed. Startup
   is bounded to 30 seconds; individual start/status/renew requests time out
   after 10 seconds. Concurrent and outdated observations are fenced. Transient
   request errors trigger observation rather than blind repeated acquisition.
2. **Backend dispatch and runner readiness.** The backend authorizes the user
   and organization, checks workspace availability and runner presence, resolves
   the runner epoch, and dispatches the revision-bound start task. The runner
   activates a durable viewer lease under the desktop lock before guest effects.
   Starting Xvnc waits for the X11 socket and TCP port 6901. Xfce startup runs
   once per desktop start. A subsequent binding probe performs `XOpenDisplay`
   and checks port 6901 before the held lease/start result is announced.
3. **Routing state.** Runner desktop events and heartbeats populate the backend
   route cache. Epoch and process generation checks reject stale updates;
   stopped-generation tombstones prevent a late active event from resurrecting
   a retired route. Global desktop activity does not imply that this tab owns
   a viewer lease.
4. **Viewer HTTP.** The iframe requests `/ws/desktop/{workspace_id}/` with its
   access token and viewer association. The ASGI proxy authenticates the JWT or
   workspace-scoped signed cookie, applies workspace access and lifecycle
   checks, and resolves the current runner. It redirects to `vnc.html` with a
   query-encoded WebSocket path, local scaling, and Kasm auto-reconnect disabled.
   HTTP resources travel through the runner to the guest's KasmVNC HTTP server.
   The backend injects the idle guard into `vnc.html`, before vendor scripts.
5. **Viewer WebSocket.** The backend registers a queue tied to the runner ID and
   Socket.IO SID, then requests a runner tunnel. The runner tracks the opening
   operation before its first admission wait, snapshots desktop generation,
   and connects to `/websockify`. It checks invalidation and generation again
   after the handshake. Only an explicit successful ACK allows the backend to
   accept the browser WebSocket. Frames then pass in both directions through
   the runner transport; binary payloads use base64 on that transport.
6. **Liveness and teardown.** The proxy monitors workspace lifecycle availability
   and renews an associated existing viewer lease every 45 seconds. The webapp
   also renews retained intents every 45 seconds. Renewal never acquires a new
   lease. Runner disconnection now closes browser tunnels for that exact SID.
   Failed handshakes, browser disconnects, cancellation, desktop replacement,
   and control-transport shutdown retire pending or active runner resources.
7. **Browser recovery.** `DesktopSurface` treats a document load separately from
   a live connection. It accepts status messages only from its iframe and
   origin and observes `noVNC_connected` when the document is accessible. A
   15-second viewer timeout and retries after 1, 2, and 4 seconds bound recovery;
   exhaustion exposes Retry. Recovery reconciles the existing intent before
   remounting. Hidden tabs pause viewer timers. Sidebar/modal transitions keep
   the same stationary iframe. A remount now reads the current token, and an
   inaccessible cross-origin document is treated as unknown connection state.

The last local owner releases its intent after a 1.5-second navigation grace
period. A replacement surface can retain it during that interval. Explicit
close sends the same revision's release tombstone even when start is in flight.

## Reproduced defects and fixes

| Failure and reproduction | Previous behavior | Fix and coverage |
| --- | --- | --- |
| Open RPC timeout, exception, cancellation, or browser failure during accept | Backend removed its local queue without telling the runner to retire the tunnel. A delayed successful open could leave an invisible VNC client. | Shielded failed-handshake cleanup sends runner close even without an ACK. Four backend regression cases reproduce the old leak. |
| Close/stop/transport loss while the runner awaits an upstream handshake | Pending opens were absent from active tunnel bookkeeping and could survive teardown. Generation was sampled after connection. | Track pending resources, invalidate them on close/workspace/transport teardown, snapshot generation before waiting, and reject stale completion. Tests cover delayed handshakes, replacement, and cancellation. |
| Close or cancellation while opening waits for lifecycle admission | Queued opening operations were invisible to cleanup until admission completed. A timeout close could miss them, allowing a later orphan connection. | Register the opening operation before the first await; delay session creation until admission; reject invalidated operations before dialing. Five additional regression cases cover closure, cancellation, and generation changes during admission. |
| Runner upstream open raises a connection error | The Socket.IO handler raised without a failure ACK, forcing the backend to wait for its RPC timeout. | Return a sanitized failure ACK; backend rejects unsuccessful or malformed ACKs before browser acceptance. |
| Runner Socket.IO disconnect, including a superseded SID | Existing browser sockets could remain apparently connected while no runner transport served them. | Close queues belonging to the lost SID. Regression coverage proves that replacement-SID connections remain intact. |
| Real gzip HTTP response from an isolated upstream | aiohttp returned decompressed bytes while the proxy retained compressed `Content-Encoding` and `Content-Length` headers. | Remove those upstream representation headers and compute length from the forwarded bytes. Real HTTP gzip regression verifies payload and headers. |
| Access token rotates while proxy URL and intent stay unchanged | Viewer remount changed its key but reused a cached computed URL containing the old token. | Compute remount key and authenticated URL together. Unit and live browser tests verify recovery with the rotated token. |
| Replacement viewer intent starts while an older active status is retained | The old proxy route remained mounted with an unconfirmed replacement revision. | Withdraw the old route until the replacement lease is observed; a deferred-start regression verifies the transition. |
| Cross-origin viewer document cannot be inspected | Missing document access was treated as disconnected; direct Window listener binding could throw. Healthy trusted status could be overwritten on load or visibility changes. | Separate unknown from disconnected, bind document listeners only when accessible, and preserve trusted status. Focused component tests cover these paths. |

## Interpretation of the supplied console messages

- **`SyntaxError: Unexpected token '{'`: unresolved attribution.** The actual
  KasmVNC client and the injected idle guard parsed successfully in both browser
  engines throughout the live runs, including retries. The idle guard is valid
  JavaScript. This audit therefore does not claim that the reported parser error
  caused the connection failures or that its exact source was fixed. A future
  occurrence needs the failing script URL, line, and column; existing viewer
  error instrumentation records safe script coordinates without logging tokens.
- **WebSocket `1006` / connection lost:** consistent with an interrupted or failed
  connection; the ownership and recovery defects above were reproduced
  independently. The live tests deliberately break upstream connections and
  reject opens, then verify successful recovery.
- **`Unix relay subscription failed, No such unix channel`:** also observed with
  a healthy connected KasmVNC desktop. It is not sufficient evidence of a failed
  desktop WebSocket.
- **`interact.min.js.map` 404:** a missing developer source map does not prevent
  the associated JavaScript from executing and is not evidence of the transport
  failure.
- **Dialog description warning:** an accessibility diagnostic, with no
  demonstrated link to desktop transport failure in this investigation.

## Verification and evidence

| Check | Result |
| --- | --- |
| Runner targeted suites | 175 passed |
| Backend relevant suites | 177 passed |
| Webapp focused desktop suites | 99 passed |
| Webapp complete suite | 2,256 tests passed in all 210 test files (`npm test -w webapp -- --reporter=dot`) |
| Webapp type checking and production build | Passed (`npm run build -w webapp`) |
| Chromium and WebKit live browser checks | 16 scenarios passed; no script parser errors |
| Physical local Socket.IO integration | 3 scenarios passed; real RFB traffic, failure ACK, disconnect cleanup |
| Formatting/static checks | Desktop/backend Ruff, changed Vue/TS ESLint and Prettier, and diff whitespace checks passed. The two existing runner control-adapter files retain baseline lint findings; comparison against HEAD found no new findings. |

Each browser engine completed ten cold page connections, ten sidebar/modal
transitions without replacing the iframe, automatic recovery from upstream
closure, recovery after two rejected opens, token rotation on reconnect,
retry exhaustion followed by manual recovery, parser-error checks, and complete
tunnel cleanup after browser closure. These are eight scenario groups per
engine, sixteen total; the repeated connections/transitions are inside them.
A separate headed Chromium check used the final code to click the KasmVNC canvas
and press `a`. An `xev` listener on display `:91` recorded `ButtonPress`,
`ButtonRelease`, `KeyPress`, and `KeyRelease`, confirming that pointer and
keyboard input crossed the browser and both proxy layers into the real X11
server.
A separate cross-origin viewer check used backend port 8000 and parent port
5173: the parent could not access the iframe DOM. After dispatching iframe-load
and visibility-change events, the connected viewer remained mounted with no
recovery for 16 seconds, beyond the connection timeout.

The live setup used the actual Vue desktop components and coordinator through
Vite, the actual backend ASGI desktop proxy, the actual runner `DesktopProxy`,
and a real local KasmVNC 1.3.3 server with an X11 desktop. Authentication,
workspace routing, lease/control-plane responses, and the backend-to-runner
Socket.IO calls were replaced with an isolated direct bridge for deterministic
fault injection. It did **not** exercise a remote runner, the physical
backend-to-runner Socket.IO network, production authentication, or VM lifecycle
operations in the browser. Authorization, lifecycle rules, and transport handlers
have separate automated coverage; the checks do not establish live remote-network
or production end-to-end verification. Linux WebKit
coverage also does not establish identical behavior on every macOS Safari
version or on the user's LAN.

A separate integration check replaced the direct bridge with an actual local
Socket.IO network connection. It used the registered backend handlers, the real
runner `WebSocketInterface` constructor and desktop/disconnect handlers, the
backend ASGI WebSocket proxy, the runner `DesktopProxy`, and an isolated KasmVNC
server on display `:91`. The runner's connect callback skipped unrelated VM
inventory/heartbeat loops. Identity, workspace routing and generation remained
fixtures; no VM lifecycle operations or production authentication were used.
The three checks confirmed:

- A binary `RFB 003.008` banner crossed KasmVNC → runner → Socket.IO → backend →
  client. Sending the banner back produced the real RFB security negotiation
  bytes `[1, 1]`, proving the reverse path.
- A refused upstream connection returned the actual runner handler's sanitized
  `ok: false` ACK over Socket.IO in 2 ms, rather than waiting for the RPC timeout.
- Disconnecting the real runner transport closed the browser WebSocket with
  code `1011`; backend, active-runner, and pending-runner tunnel counts all
  returned to zero.

This closes the local Socket.IO transport validation gap left by the browser
fixture. It does not verify a remote network, production authentication or VM
lifecycle behavior end to end.

The initial complete webapp run had four failures caused by the absent local
Monaco dependency. Running `npm ci` at the repository root restored dependencies
from the existing lockfile without changing manifests or the lockfile. The full
suite, type check, and production build then passed.

Evidence retained in the workspace:

- [Physical Socket.IO results](/workspace/reports/desktop-connection-audit/socketio-network-results.json)
- [Physical Socket.IO test script](/workspace/reports/desktop-connection-audit/socketio-network.py)
- [Physical Socket.IO test log](/workspace/reports/desktop-connection-audit/socketio-network.log)
- [Supplementary cross-origin/input results](/workspace/reports/desktop-connection-audit/extra-checks.json)
- [Browser fixture](/workspace/reports/desktop-connection-audit/fixture.html)
- [Browser results](/workspace/reports/desktop-connection-audit/browser-results.json)
- [Browser test script](/workspace/reports/desktop-connection-audit/browser.mjs)
- [Isolated ASGI/control bridge](/workspace/reports/desktop-connection-audit/server.py)
- [Chromium connected desktop](/workspace/reports/desktop-connection-audit/chromium-connected.png)
- [WebKit connected desktop](/workspace/reports/desktop-connection-audit/webkit-connected.png)
- [Backend test log](/workspace/reports/desktop-connection-audit/backend-tests.log)
- [Runner test log](/workspace/reports/desktop-connection-audit/runner-tests.log)
- [Webapp test log](/workspace/reports/desktop-connection-audit/frontend-tests.log)

Changes span webapp, backend, and runner. Updating all three components is
required to obtain the complete behavior described here. The original
reported incident remains unattributed beyond the reproducible defects and
the stated test boundaries.
