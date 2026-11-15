# OpenCuria Mobile

Native iOS and Android client (Expo SDK 57, React Native 0.86.3). It talks directly
to the OpenCuria server and uses platform controls: SwiftUI/UIKit on iOS, Material
on Android (`@expo/ui`, native menus, native navigation and text editing).

## Features

- Workspaces and chats: live output, subtasks, permission/question gates, Markdown,
  mentions, skills, model choice, photo/camera/document attachments.
- Workspace tools: Git, the KasmVNC desktop (trackpad and keyboard), terminal, files.
- Apps published by agents (`app_publish`): installed and run inside OpenCuria like
  in Expo Go, also offline, with an app menu (shake, three fingers or menu button)
  and a home screen widget. See
  [`modules/opencuria-app-runtime`](modules/opencuria-app-runtime/README.md).
- Scheduled tasks, push notifications, iOS Live Activities and every webapp setting.
  MCP OAuth connections finish in the browser.
- Offline mode: opened chats and their pictures are cached per account
  (`src/offline/`) and shown read-only when the server is unreachable.

## Local development

Requirements: Node.js 22.13+, Java 17, Android SDK Platform 36, Build-Tools 36.0.0
and NDK 27.1.12297006 (`ANDROID_HOME` set). iOS builds need macOS, Xcode and CocoaPods.

```sh
npm ci                      # repository root, once
export APP_VARIANT=development EXPO_PUBLIC_DEFAULT_SERVER_URL=http://10.0.2.2:8000
(cd mobile && npx expo run:android)   # build and install the dev client
npm run dev:mobile                    # Metro for the dev client, not Expo Go
```

Use `npx expo run:ios` on a Mac. Rebuild the development client after native
dependency changes.

`EXPO_PUBLIC_DEFAULT_SERVER_URL` only prefills the sign-in screen and is public; never
put secrets in it. `10.0.2.2` reaches the host from the Android emulator; for a USB
device use `adb reverse tcp:8000 tcp:8000` and `adb reverse tcp:8081 tcp:8081`
with `http://127.0.0.1:8000`. Plain `http://` is accepted only for local-network
hosts; public hosts need `https://`.

## Repository map

- `app/`: Expo Router routes only. Screens live in `src/screens/`; never add `src/app/`.
- `src/state/chatController.ts`: REST hydration, live subscriptions, immutable chat snapshots.
- `src/components/`, `src/ui/`: chat presentation and native controls.
- `src/tools/`: workspace tools sheet (Git, desktop, terminal, files).
- `src/settings/`: settings sheet and pages.
- `src/apps/`: installing and opening published apps. After adding or upgrading a
  native dependency run `node mobile/scripts/sync-app-runtime.mjs`.
- `src/scheduledTasks/`, `src/notifications/`, `src/widgets/`, `src/offline/`,
  `src/platform/` (secure storage, pickers, uploads, media cache).
- `modules/`: local Expo modules (app runtime, terminal, highlighter, PDF, toolbar).

## Checks

From the repository root:

```sh
npm run type-check
npm run test
npm run export -w mobile    # JS bundle for both platforms, not a signed build
```
