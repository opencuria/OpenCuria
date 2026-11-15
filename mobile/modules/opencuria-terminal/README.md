# Native terminal surface

Local Expo module with the native terminal view of the workspace tools sheet. It is adapted from
t3code's `apps/mobile/modules/t3-terminal` (MIT, Copyright (c) 2026 T3 Tools Inc.; see `LICENSE`
and `THIRD_PARTY_NOTICES.md`). It renders and edits a terminal only; the PTY protocol, session
lifetime and toolbar live in `mobile/src/tools/terminal/`.

The JavaScript contract (`OpenCuriaTerminalSurface` view) is intentionally small:

- `writeOutput(data, reset)` feeds decoded PTY output; `reset` recreates the terminal with `data`
- input from the native surface is emitted as `onInput { data: string }`
- the fitted grid is emitted as `onResize { cols: number, rows: number }`
- `captureRequest` (increasing number) emits the visible rows as `onCapture { text: string }`
- props: `terminalKey`, `fontSize`, `focusRequest`, `autoFocus`, `readOnly`, `appearanceScheme`,
  `themeConfig`, `backgroundColor`, `foregroundColor`, `mutedForegroundColor`, `sharedGrid`

iOS uses the vendored `GhosttyKit.xcframework` (libghostty with custom I/O): one Ghostty app and
surface per view, remote output fed with `ghostty_surface_feed_data`, user input returned through
the write callback. Android uses upstream `libghostty-vt` for terminal state, parsing, reflow and
scrollback; a Canvas view renders compact snapshots produced by the JNI bridge
(`android/src/main/cpp/opencuria_terminal_jni.cpp`).

Expo autolinks local modules from `mobile/modules/`; rebuild the development client after changes.

## Rebuilding the vendored libraries

`GhosttyKit.xcframework` is built from Yash Singh's Ghostty fork at revision
`cf8edc23f3a6a87a96e41a90013e89e987d34980` (`Vendor/libghostty/VERSION`). Set `GHOSTTY_SOURCE_DIR`
to a clone of https://github.com/Yash-Singh1/ghostty at that revision and run
`scripts/build-libghostty-ios16.sh` on macOS with Xcode's Metal toolchain installed.

The Android shared libraries in `android/src/main/jniLibs/` and the C headers in
`Vendor/libghostty-vt/include` are pinned to the upstream revision in
`Vendor/libghostty-vt/VERSION`. Set `ANDROID_NDK_HOME` and run `scripts/build-libghostty-android.sh`;
it downloads Zig when needed, applies `scripts/libghostty-android-patches/` and rebuilds all four
ABIs with 16 KB page-size support.
