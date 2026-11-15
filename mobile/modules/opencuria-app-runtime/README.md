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
- **Sandbox:** each app has its own document, cache, SQLite and SecureStore scope.
  Notifications, AsyncStorage and background tasks are not scoped.
- **Workspace backends:** `AppGateway` rewrites `http://<process>.opencuria.local[:port]`
  to the API gateway and adds the session token, which the app never sees. Android
  routes clients created through `OkHttpClientProvider` only.
- **App menu** (`AppMenuTriggers`, `AppMenuButton`): shaking, a three-finger long
  press or a draggable menu button opens Close app and Reload. Each way can be turned
  off, but never the last one; the setting applies to every app.
- **One app at a time.** A fatal JavaScript error shows Reload/Close instead of
  ending OpenCuria.

JavaScript API (`src/apps/appRuntime.ts`): `open(request)`, `close()`,
`clearData(appId)` and, on Android, `updateAppsWidget(snapshot)`. Rebuild the
development client after native changes. The iOS side has not been built in CI.
