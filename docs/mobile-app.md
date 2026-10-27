# OpenCuria Mobile app

## Product boundary

`mobile/` is a native iOS/Android chat app for an existing OpenCuria server. It follows the native-control boundaries in the workspace’s `t3code/apps/mobile/` reference: SwiftUI/UIKit on iOS, Jetpack Compose/Android on Android. OpenCuria keeps its workspace/chat structure and shared chat protocol, data types and REST/Socket.IO transport. See [Native chat design](mobile-chat-design.md) for the control and navigation mapping.

**Included:** sign-in, organization selection, workspace/conversation browsing, persisted history, create/open chats, streaming harness turns, abort/edit/fork/rename/delete session actions, live task/subtask status, permission/question gates, model/effort/harness/mode/skill selection, mention search, Markdown and workspace file references, photo/camera/document attachments, bounded file/media preview/share, optional native push notifications, and iOS Live Activities. Workspace operations exposed by the client library are not the same as app features: mobile intentionally does not offer workspace create/update/delete. Mobile can delete a conversation/session, but not its workspace. Offline chat history and send queues remain out of scope.

The app suspends Socket.IO while backgrounded and reactivates it in foreground, then refreshes sessions, overview and active chat using REST. Socket.IO reconnect retains logical subscriptions, but there is no durable event log/replay guarantee; a temporarily unavailable server requires a foreground retry. Native notifications and Live Activity updates use APNs/FCM independently of that connection. Sign-out revokes this installation's notification destinations before clearing local auth; JWT session revocation remains separate. User-facing copy is English. See [Mobile notifications](mobile-notifications.md) for provider setup, lifecycle, privacy and device acceptance.

## Architecture

```text
Expo Router (`mobile/app/` routes only)
  └─ `mobile/src/screens/` login/session + native app screen
      ├─ `mobile/src/state/chatController.ts` immutable chat state, REST hydration, live events
      ├─ `mobile/src/components/` native composer, transcript, gates, Markdown
      └─ `mobile/src/platform/` SecureStore, Expo pickers, uploads, private media cache
            └─ `@opencuria/client` ─ REST `/api/v1` + Socket.IO `/frontend`, path `/ws/runner`
                  └─ `@opencuria/chat-core` ─ shared harness protocol/reducer/display helpers
```

- **Shared code:** `packages/chat-core` contains framework-neutral chat protocol/data and reducers; `packages/client` is the typed API/auth-refresh core used by both clients plus the mobile Socket.IO/file-transfer adapter. Mobile must follow actual server contracts rather than independently recreating web-specific harness logic.
- **Screen/state boundary:** Screens compose native controls and call the shared client. `ChatController` owns one selected workspace/session's transcript, hydrates from REST and applies live updates from Socket.IO. It publishes immutable snapshots (`useSyncExternalStore`); native React consumers must not mutate shared messages, parts or reducer state. The controller subscribes to harness status/part, subtask, todo and gate events. The app screen subscribes to workspace status/operation and conversation-overview events; both refresh relevant REST data after reconnect/foreground.
- **Routing:** Expo Router uses `mobile/app/` only for route entry/layout. Put application feature screens under `mobile/src/screens/`, never `mobile/src/app/`; the latter would be discovered as a conflicting Expo Router route tree.
- **Transport:** REST uses `<server-prefix>/api/v1`. Socket.IO connects to the `/frontend` namespace at the server origin with Engine.IO path `<server-prefix>/ws/runner`. The shared client separates namespace and reverse-proxy prefix explicitly; there is a regression test for non-root installations. The webapp retains its desktop/terminal socket adapter; shared REST, DTOs, reducers and file protocol helpers are reused rather than importing Vue in native code.
- **Authentication:** Password is used only for login and not saved. The access/refresh tokens, server URL and selected organization ID are serialized in Expo SecureStore. REST credentials are explicitly omitted; bearer tokens and organization context are attached by the client. Sign-out clears the local secure record, disposes realtime and scoped media/cache resources. Do not add secrets as `EXPO_PUBLIC_*` variables: Expo embeds those in the app bundle.
- **Native media:** `src/platform/pickers.ts` handles camera, photo library and document picker. Uploads use the existing authenticated chunked file transport and shared `@file:` mention behavior, not an anonymous HTTP endpoint. HEIC/HEIF photos are converted to bounded JPEG; other files are not recompressed. `MediaCache` validates workspace paths and stores previews in a random app-private cache scope, with no account/workspace identifiers in filenames.

## Development and API endpoint setup

Requirements and basic commands are in [`mobile/README.md`](../mobile/README.md). Install all npm workspaces from the repository root with `npm ci`; the root `package-lock.json` replaces the former webapp-local lockfile. Native builds use a **development client**, not Expo Go. Expo SDK `~57.0.30` / React Native `0.86.3` are the versions pinned in `mobile/package.json`.

| Target | Development prerequisites |
| --- | --- |
| Android | Node 22.13+, Java 17, Android SDK Platform 36, Build-Tools 36.0.0, NDK 27.1.12297006; set `ANDROID_HOME`/`ANDROID_SDK_ROOT`; `cd mobile && APP_VARIANT=development npx expo run:android`. |
| iOS | macOS, Xcode and CocoaPods; `cd mobile && APP_VARIANT=development npx expo run:ios`. A physical device also needs a provisioned Apple development signing identity/profile and a server/Metro URL reachable from that device. |

`EXPO_PUBLIC_DEFAULT_SERVER_URL` optionally presets the editable server URL on login; it is public configuration, not a secret. For an Android emulator, the host machine is `10.0.2.2`, not `localhost`; on a physical phone use a LAN-reachable host address and allow the development API/Metro ports through the host firewall. Every build allows plain HTTP to local-network servers only (loopback/private/link-local IPs, `localhost`, `.local` and unqualified host names such as `http://nas:8000`); a scheme-less local host defaults to `http://`, anything else to `https://` and public hosts must use HTTPS. iOS enforces this through `NSAllowsLocalNetworking`; Android cannot limit cleartext to IP ranges, so `usesCleartextTraffic` is on and `normalizeServerUrl` is the guard. The server URL accepts an origin plus optional reverse-proxy path and rejects query, fragment and embedded credentials. Do not append query parameters to configure a proxy. An `/api/v1` suffix is normalized without doubling it; deploy the Socket.IO `/frontend` namespace at `<same-origin-and-prefix>/ws/runner` as well. Verify your actual reverse-proxy/socket route if using a path prefix.

`APP_VARIANT` is fail-closed: an unset value selects production, and only exactly `development` enables the `.dev` application ID. The npm development scripts set that value explicitly. For production native generation use `npm run prebuild:production -w mobile`; do not build a release from an old development-generated native tree. Release signing is separate. This repository does not define EAS, store submission or production signing, and does not claim an iOS native build.

## Data limits and privacy considerations

- Uploads have a **10 MiB** limit per file, verified before base64 allocation and again against encoded content. Uploads require a live workspace Socket.IO connection. Picked document copies and converted photos are temporary local files; they are not cleared at every composer cancellation. Treat OS-managed picker caches as transient and never interpret them as offline drafts.
- Workspace preview/text reads are capped at **20 MiB**. Media-preview LRU cache is capped at **32 MiB**, with at most two concurrent transfers. Binary documents up to **100 MiB** are downloaded only on explicit user request; sharing uses the native system sheet. Preview cache and downloaded temporary files are app-private, scoped to the current media-adapter lifetime and removed on normal scope disposal/logout. The OS may reclaim caches early; abrupt termination/crash/force-stop may prevent cleanup. They are not an encrypted persistent file store.
- Auth tokens use platform SecureStore, not chat data. The server remains the source of truth for workspaces, sessions, conversation history and uploaded files. Do not store transcript snapshots, passwords, API keys, credentials, or durable workspace identifiers in local storage, logs or public Expo config.
- Live chat uses REST for hydration and Socket.IO for updates. A foreground refresh reconciles persisted sessions/history, but disconnected sends and file transfers are not queued. Native push and Live Activity updates are delivered independently by the backend's durable outbox; they contain bounded conversation/workspace metadata, never transcript, question or tool contents. Push is not offline history/replay and does not execute an agent on the phone.

## Testing and current verification status

From repo root run `npm run type-check`, `npm run test`, and `npm run export -w mobile`. The root `npm run test` gates shared chat/client tests, mobile logic/UI tests and webapp tests. CI also runs the mobile JS export. These do not replace signed Android/iOS release or device acceptance.

The [verification report](mobile-verification.md) records the Android development build, native screenshots and remaining platform limits. iOS JavaScript/Hermes export passed; an iOS native build and device acceptance still require a Mac. No physical-device, signing or store acceptance is claimed.
