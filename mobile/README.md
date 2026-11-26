# OpenCuria Mobile

Native iOS and Android client (Expo SDK 57, React Native 0.86.3). It talks directly
to the OpenCuria server and uses platform controls: SwiftUI/UIKit on iOS, Material
on Android (`@expo/ui`, native menus, native navigation and text editing).

## Features

- Workspaces and chats: live output, subtasks, permission/question gates, Markdown,
  mentions, skills, model choice, photo/camera/document attachments. Messages sent
  while the agent works steer its turn; holding Send schedules a message for later.
- Workspace tools: Git, the KasmVNC desktop (trackpad and keyboard), terminal, files.
- Apps published by agents (`app_publish`): installed and run inside OpenCuria like
  in Expo Go, also offline, with a home screen widget. Shaking, three fingers or the
  menu button open the app companion: a half or full height sheet with the chats of
  the app's workspace (the same chat as everywhere else) and Record, which records
  speech while the user taps (Use) or points (Point) in the app. The chat's draft
  then references a `timeline.md` with timestamped speech, taps, screenshots, logs
  and requests. See [`modules/opencuria-app-runtime`](modules/opencuria-app-runtime/README.md).
- Scheduled tasks, push notifications, iOS Live Activities and every webapp setting.
  MCP OAuth connections finish in the browser.
- Dictation as in the webapp: the microphone takes Send's place while the draft
  is empty. Recordings are saved on the device first (`src/voice/`), wait there
  through lost connections and restarts, and their text goes to the draft of
  the chat they were recorded in. Sign out deletes them.
- Offline mode: opened chats and their pictures are cached per account
  (`src/offline/`) and shown read-only when the server is unreachable. Composer
  drafts (one per chat and engine, as in the webapp) are kept there too, never in
  AsyncStorage, which installed apps share; sign out deletes both.

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

Reanimated stays at 4.5.5, above SDK 57's 4.5.1, whose settled animations can snap
back to stale values. The root `overrides` keep one copy and `expo.install.exclude`
stops `expo install --fix` from downgrading it; drop both with the next SDK upgrade.

`react-native-enriched-markdown` is configured in `package.json` (`enriched-markdown`),
not as an Expo plugin. Its video player stays off: chat videos play through `expo-video`.

`EXPO_PUBLIC_DEFAULT_SERVER_URL` only prefills the sign-in screen and is public; never
put secrets in it. `10.0.2.2` reaches the host from the Android emulator; for a USB
device use `adb reverse tcp:8000 tcp:8000` and `adb reverse tcp:8081 tcp:8081`
with `http://127.0.0.1:8000`. Plain `http://` is accepted only for local-network
hosts; public hosts need `https://`.

## Repository map

- `app/`: Expo Router routes only. Screens live in `src/screens/`; never add `src/app/`.
  `index.ts` also registers `OpenCuriaAppCompanion`, a second React root drawn over
  open apps; roots share one session (`SessionScope`), draft store and voice queue.
- `src/state/useWorkspaceChat.ts` and `src/components/WorkspaceChatView.tsx`: one
  workspace chat, used by `MainScreen` and `AppCompanionScreen`.
- `src/state/chatController.ts`: REST hydration, live subscriptions, immutable chat snapshots.
- `src/components/`, `src/ui/`: chat presentation and native controls. One design
  language everywhere: ink on a white/black canvas (`theme.primary` is the text
  color), color only for status (`ui/statusColors.ts`) and destructive actions,
  shared list states in `ui/formStates.ts`. Content on liquid glass takes its
  colors from `useInk()` (`ui/ink.tsx`): small glass turns light or dark with
  what lies behind it, whatever the theme.
- `src/tools/`: workspace tools sheet (Git, desktop, terminal, files).
- `src/settings/`: settings sheet and pages.
- `src/apps/`: installing and opening published apps; `src/apps/companion/` holds
  the companion's sheet, recording bar and recorder. After adding or upgrading a
  native dependency run `node mobile/scripts/sync-app-runtime.mjs`.
- `src/voice/`: dictation (expo-audio capture, saved recordings, transcription
  queue from `@opencuria/chat-core/voiceTranscriptions`).
- `src/scheduledTasks/`, `src/notifications/`, `src/widgets/`, `src/offline/`,
  `src/platform/` (secure storage, pickers, uploads, media cache).
- `modules/`: local Expo modules (app runtime, terminal, highlighter, PDF, toolbar).
- `plugins/`: local config plugins (app extensions take the app's version and build number).

## Checks

From the repository root:

```sh
npm run type-check
npm run test
npm run export -w mobile    # JS bundle for both platforms, not a signed build
```

## TestFlight

`.github/workflows/testflight-dev.yml` uploads `dev` nightly when the app changed,
or on demand (Actions → TestFlight (dev) → Run workflow). It uses the `production`
profile of `eas.json` with credentials and build numbers stored on EAS; the
secrets it needs are listed at the top of the workflow.
