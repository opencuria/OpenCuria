# OpenCuria Mobile

Native iOS and Android chat client built with Expo SDK 57 and React Native 0.86.3. The interface follows t3code’s use of platform controls: SwiftUI/UIKit on iOS and Material/Android controls on Android, with native navigation, text editing, menus and file pickers; it talks directly to the configured OpenCuria server, not to the webapp.

## Scope

- Browse existing workspaces and conversations, read history, and start or continue harness chats; follow live output, tasks/subtasks, permission and question gates, Markdown, mentions, model choices and skills.
- Attach photos, camera images and documents; preview supported workspace files. Chat and workspace data remain on the server.
- Receive optional native push notifications for finished/failed runs and pending approvals/questions. On iOS, follow running agents on the Lock Screen and Dynamic Island with Live Activities. See [Mobile notification setup](../docs/mobile-notifications.md) for Apple/Firebase credentials and native build requirements.
- Sign in to an existing server. The app does **not** create/delete workspaces or provide workspace CRUD or offline chat/queue/replay. Chat refreshes from REST when brought back to the foreground; push delivery does not replay missed chat events.

## Native interface

Buttons, selection rows, model/engine pickers and lists use platform controls via `@expo/ui`. Menus use `@react-native-menu/menu` (`UIMenu` / Android popup menus); no Expo bottom-sheet component is used. Modal screens use the OS presentation and native navigation bars. iOS navigation uses `NavigationSplitView`; Android uses `DrawerLayoutAndroid`. The composer uses a native `UITextView` / `EditText`, with `expo-glass-effect` on supported iOS versions and a Material surface on Android.

The sidebar shares workspace eligibility, inbox and sorting rules with the webapp through `@opencuria/chat-core/conversationGroups`. Each workspace initially shows four recent chats, with independent four-chat pagination. Closing navigation retains pagination and collapsed groups; switching organization resets them. Stopped workspaces remain accessible through All workspaces. Scheduled tasks are not shown.

These native dependencies require rebuilding the development client after updating the branch.

## Local development

Requirements: Node.js **22.13+**, Android Studio command-line tools and an Android SDK, and **Java 17**. For Android, install SDK Platform **36**, Build-Tools **36.0.0**, and NDK **27.1.12297006**. Set `ANDROID_HOME` (and, if needed, `ANDROID_SDK_ROOT`) to the SDK location. iOS builds require macOS, Xcode and CocoaPods.

Install JavaScript dependencies once from the repository root; the root npm workspace and `package-lock.json` cover web, mobile and shared packages:

```sh
cd /path/to/OpenCuria
npm ci
```

Build and install the Android development client (from `mobile/`):

```sh
cd mobile
APP_VARIANT=development EXPO_PUBLIC_DEFAULT_SERVER_URL=http://10.0.2.2:8000 npx expo run:android
```

Then start Metro with the **development client**, not Expo Go:

```sh
cd /path/to/OpenCuria
APP_VARIANT=development EXPO_PUBLIC_DEFAULT_SERVER_URL=http://10.0.2.2:8000 npm run dev:mobile
```

`EXPO_PUBLIC_DEFAULT_SERVER_URL` is only an optional, build-time default for the editable server URL on the sign-in screen. It is public app configuration—never put passwords, API keys or tokens in it. For an Android emulator, `10.0.2.2` reaches the host machine; a physical phone needs a LAN-reachable host name/IP, an accessible firewall port and Metro reachable over the LAN. For a USB-connected Android device, `adb reverse tcp:8000 tcp:8000` and `adb reverse tcp:8081 tcp:8081` can forward the API and Metro ports from the host; use `http://127.0.0.1:8000` as the server URL. `localhost` on a phone without forwarding means the phone. Plain `http://` is accepted in every build, but only for local-network servers (private/loopback IPs, `localhost`, `.local` and single-label host names); public hosts need `https://`. See [Mobile app architecture, security and testing](../docs/mobile-app.md) for proxy paths, iOS and server setup.

For an iOS simulator on a Mac, from `mobile/` run `APP_VARIANT=development EXPO_PUBLIC_DEFAULT_SERVER_URL=https://curia.example npx expo run:ios`. A physical iPhone additionally needs suitable Apple development signing/provisioning and network access to both the server and Metro. No iOS build is claimed for this workspace.

## Repository map

- `app/` — Expo Router entry/layout only. Keep feature screens under `src/` (do not introduce `src/app/`, which would collide with the router convention).
- `src/screens/` — login, authenticated app and session lifecycle.
- `src/state/chatController.ts` — REST hydration, live subscriptions, chat state and immutable snapshots for React.
- `src/components/`, `src/ui/` — native chat presentation and controls.
- `src/platform/` — secure token storage, camera/document pickers, authenticated uploads and bounded temporary media cache.
- `src/notifications/`, `src/widgets/` — device consent, native push tokens, notification navigation and the iOS Live Activity layout.
- `packages/chat-core/`, `packages/client/` — framework-neutral shared chat helpers/protocol and the real REST/Socket.IO client used by the web and native clients.

## Checks

From the repository root:

```sh
npm run type-check
npm run test
npm run export -w mobile
```

`npm run test` covers the shared packages, mobile logic/UI tests and webapp tests. `export` checks an Expo JS bundle for both platforms; it is **not** a signed native release build. An Android debug APK was built. See the [native UI verification report](../docs/mobile-native-verification.md) for current checks and screenshots, and the [initial mobile verification](../docs/mobile-verification.md) for earlier transport/platform coverage. This is not a claim of iOS native build, production-signing or store acceptance.
