# App runtime

Local Expo module that runs apps published by agents (`app_publish`) inside
OpenCuria, like Expo Go: the JavaScript bundle comes from the installed files,
native modules from OpenCuria. Installing, updating and the Apps list live in
`mobile/src/apps/`; this module opens and closes an installed version, clears its
data and draws the Android Apps widget.

- **Separate React instance:** `AppRuntimeActivity` (Android) and
  `AppRuntimeViewController` with `AppRuntimeFactory` (iOS). OpenCuria's own React
  host is never touched; `AppRuntimeFactory` hands the Fabric component provider back
  to OpenCuria when the app closes.
- **Offline:** an app-only `ExpoUpdates` module maps asset hashes to downloaded files
  and provides the app's Expo config.
- **Sandbox:** each app has its own document, cache and SecureStore scope (own
  Keystore/Keychain namespace); SQLite databases only on iOS. Native code derives
  an app's directories from its id and never shows apps OpenCuria's own modules
  (`opencuria-*`, dev client, widgets; `src/apps/appRuntimeHostModules.test.ts`).
  One file policy (`AppFileAccess`) gates Expo modules, React Native's networking
  and ImageStore and `expo/fetch`: an app reads and writes its data and cache,
  reads its installed files and nothing else of OpenCuria. On iOS the Expo part
  needs `patches/expo-modules-core+57.0.23.patch` (root `postinstall`); without it
  the runtime does not compile. It only takes effect because `app.config.ts` turns
  off Expo's precompiled frameworks (`ios.usePrecompiledModules: false`), so
  `expo-modules-core` builds from the patched sources. Do not build only some
  modules from source instead (`expo.autolinking.ios.buildFromSource`): a
  precompiled module then links `ExpoModulesCore.framework`, which the app no
  longer contains, and iOS kills the app at launch. After an Expo upgrade regenerate
  the patch with
  `npx patch-package expo-modules-core --include '^ios/FileSystemUtilities/'`.
  Notifications, AsyncStorage and background tasks are not scoped, and libraries
  that open paths on their own (expo-sqlite, WebView file access, Skia and others)
  are not confined yet.
- **Workspace backends:** `AppGateway` rewrites `http://<process>.opencuria.local[:port]`
  to the API gateway and adds the session token, which the app never sees. Android
  routes clients created through `OkHttpClientProvider` only.
- **App companion** (`AppCompanion`): shaking, a three-finger long press or the
  draggable menu button (`AppMenuTriggers`, `AppMenuButton`; never all off) show a
  surface of OpenCuria's own React instance (`OpenCuriaAppCompanion`) in a layer
  above the app. While it exists, OpenCuria's host follows the app activity's
  lifecycle (Android), so its dialogs, pickers and keyboard work over the app. The
  layer's touch mode decides who gets touches: `none` (hidden), `all` (sheet,
  pointer mode) or `regions` (only the recording bar; the app gets the rest).
  Without OpenCuria's factory (iOS: an app delegate that is no
  `ExpoReactNativeFactoryProvider`) the native menu with Close app and Reload shows.
- **Recordings** (`AppRecorder`): while observed, touches on the window are reported
  with the control under the finger (role, text, `testID`, bounds), and frames of
  the app's content are captured as JPEG (unchanged pixels and texts are skipped).
  `inspector/app-inspector.js` runs in the app's JavaScript (Android: around the
  bundle; iOS: from `host:didInitializeRuntime:`) and reports console output,
  uncaught errors and requests (no headers, bodies or query values); the latest 300
  are kept per app.
- **One app at a time.** A fatal JavaScript error shows Reload/Close instead of
  ending OpenCuria.

JavaScript API (`src/apps/appRuntime.ts`): `open(request)`, `close()`,
`clearData(appId)` and, on Android, `updateAppsWidget(snapshot)`; the companion's
functions and events are in `src/apps/companion/companionNative.ts`. Rebuild the
development client after native changes. The iOS side has not been built in CI.
