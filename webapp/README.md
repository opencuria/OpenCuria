# .

This template should help get you started developing with Vue 3 in Vite.

## Recommended IDE Setup

[VS Code](https://code.visualstudio.com/) + [Vue (Official)](https://marketplace.visualstudio.com/items?itemName=Vue.volar) (and disable Vetur).

## Recommended Browser Setup

- Chromium-based browsers (Chrome, Edge, Brave, etc.):
  - [Vue.js devtools](https://chromewebstore.google.com/detail/vuejs-devtools/nhdogjmejiglipccpnnnanhbledajbpd)
  - [Turn on Custom Object Formatter in Chrome DevTools](http://bit.ly/object-formatters)
- Firefox:
  - [Vue.js devtools](https://addons.mozilla.org/en-US/firefox/addon/vue-js-devtools/)
  - [Turn on Custom Object Formatter in Firefox DevTools](https://fxdx.dev/firefox-devtools-custom-object-formatters/)

## Type Support for `.vue` Imports in TS

TypeScript cannot handle type information for `.vue` imports by default, so we replace the `tsc` CLI with `vue-tsc` for type checking. In editors, we need [Volar](https://marketplace.visualstudio.com/items?itemName=Vue.volar) to make the TypeScript language service aware of `.vue` types.

## Customize configuration

See [Vite Configuration Reference](https://vite.dev/config/).

## Project Setup

```sh
npm install
```

### Compile and Hot-Reload for Development

```sh
npm run dev
```

### Type-Check, Compile and Minify for Production

```sh
npm run build
```

### Lint with [ESLint](https://eslint.org/)

```sh
npm run lint
```


## Clipboard behavior and checks

Use `CopyButton` for button actions and `copyToClipboard` for menu actions.
Call the helper directly in the click/key handler, before unrelated awaits.
For text loaded asynchronously, pass a producer (`() => Promise<string>`), not
an already-awaited value. This starts the secure-context ClipboardItem write
within Safari's user gesture. Plain-text HTTP copies use a focus-neutral legacy
copy event so modal menus and dialogs cannot steal the selected payload.

If the browser blocks copying, or an HTTP operation first needs to load text,
the shared dialog supplies a fresh **Copy now** gesture and selectable text.
Dismissal, unmount, and workspace changes clear pending fallback content. Copied
values never appear in notifications. Existing native writes cannot be cancelled
after the browser has received their payload; aborts suppress stale feedback.

Message copy preserves original Markdown. Assistant copy is offered after a
turn completes and contains only the trailing answer text after work/agent
blocks, matching the timeline layout. Code-block copy contains only code.

Run the isolated browser regressions (no backend/authentication needed):

```sh
cd ../e2e
npm ci
npx playwright install --with-deps chromium webkit
npx playwright test --config=playwright.clipboard.config.ts
```

The tests serve real Vue components on non-local HTTP and HTTPS origins and
verify native copy/paste in Chromium and WebKit, including menu/dialog focus
traps, deferred loading, failures, Markdown, and touch/keyboard controls.
`CLIPBOARD_VITE_URL` selects the Vite server; `CLIPBOARD_CHROMIUM_PATH` optionally
selects a system Chromium executable. WebKit coverage does not replace a final
Safari/macOS smoke test.


### Desktop clipboard

The same-origin Kasm viewer owns native Cmd/Ctrl+C/V gestures. Paste uses the
trusted `ClipboardEvent.clipboardData` (including WebKit's empty `types` list),
not `navigator.clipboard.readText()`. A temporary invisible editable target
ensures Safari dispatches paste over the canvas. Text is written through the
existing authorized VM endpoint before one balanced Linux Ctrl+V is forwarded;
Ctrl+Shift+C/V is preserved for guest terminals. Local editors are excluded.

With the modern API, copy starts a promise-backed ClipboardItem write in the
**focused iframe's** realm during the original gesture, then forwards guest
Ctrl+C. The guest's `clipboardrx` event supplies fresh text; a bounded REST read
covers identical copies without a new guest event.

On HTTP, where `navigator.clipboard` is absent, the shortcut instead makes one
**synchronous** authenticated clipboard-read request with `copy_selection: true`.
The runner arms an XFixes listener, sends the Linux copy shortcut and returns
only text acknowledged by a new guest clipboard ownership event. Plain-text
clipboard ownership is temporarily replaced with the same text so identical
copies remain observable; rich clipboards are not replaced. The browser then
executes its native copy command in the original key-handler call stack. There
is no second iframe Ctrl+C and no Copy dialog. Server work is bounded to four
seconds (the guest script has its own shorter deadline). Synchronous XHR cannot
set a main-thread timeout, so a network/proxy stall can still block the viewer
until the browser's network timeout; HTTPS keeps the nonblocking modern path.
Failed reads or denied browser writes preserve the host clipboard, never copy
an old snapshot, and report a generic error.

The existing clipboard-read endpoint accepts an optional strict body
`{ "copy_selection": true, "shift": false }`; bodyless reads retain their snapshot
semantics. Both modes require `terminal:access`, organization membership and
workspace ownership (or organization admin). Selection copy also respects
capture/computer-use fencing. The matching MCP tool is `read_desktop_clipboard`.
Text is the supported shared clipboard format; image/file pastes are not sent
as stale text. Desktop shortcuts never open the Copy dialog.

```sh
cd ../e2e
npx playwright test --config=playwright.desktop-clipboard.config.ts
```

This isolated suite uses native host copy/paste in Chromium and WebKit on HTTP
and HTTPS, with delayed/failed writes, repeated large Unicode text, guest key
normalization, read-only computer-use, and stale iframe remounts. Real macOS
Safari still requires a platform smoke test; Linux WebKit is not Safari on Mac.
