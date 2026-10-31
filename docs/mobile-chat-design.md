# Native chat design

The native-control reference is `t3code/apps/mobile/` in the workspace. OpenCuria keeps its chat, workspace and account structure while using actual platform controls for interaction. The reference stays read-only; the workspace tools' terminal surface, syntax highlighter and file browser are adapted from it under its MIT license (see [Workspace tools](#workspace-tools) and `THIRD_PARTY_NOTICES.md`).

## Platform mapping

| Element | iOS | Android |
| --- | --- | --- |
| Buttons | SwiftUI Button and SF Symbols | Material Button/IconButton |
| Rich transcript actions | SwiftUI Button | Material clickable Surface |
| Chat editor | Native multiline UITextView | Native multiline EditText |
| Composer surface | Native liquid glass where supported | Material Surface |
| Attachment and message menus | UIMenu | Android PopupMenu |
| Model, effort and agent selection | SwiftUI sheet with detents and List rows | Material 3 ModalBottomSheet with list rows |
| Question options | Standard checkmark selection rows | Material RadioButton/Checkbox rows |
| Sidebar rows | Compact native views with UIContextMenu on long press | Compact native views with ripple and PopupMenu on long press |
| Workspace picker list | SwiftUI List/Section | Material LazyColumn/ListItem |
| Navigation | iPhone: sidebar sliding in beneath the chat; iPad: adaptive NavigationSplitView | Sidebar sliding in beneath the chat |
| Modal screens | UIKit page-sheet presentation | Android system modal dialog |
| Chat header | Transparent UINavigationBar with bar button items and UIMenu | Transparent Toolbar with tonal icon buttons and PopupMenu |
| Modal headers | UINavigationBar | Android Toolbar |
| Login fields | SwiftUI TextField/SecureField | Material OutlinedTextField |

`@expo/ui` bridges SwiftUI and Jetpack Compose primitives. Its universal `BottomSheet` presents the model sheet as a native SwiftUI sheet (detents, drag indicator) or Material 3 ModalBottomSheet; no JavaScript sheet gesture, simulated handle or custom drawer animation is used. `@react-native-menu/menu` supplies OS context menus, and `expo-glass-effect` supplies the native iOS composer surface. These dependencies require a new development-client build.

Android modal headers use the local `mobile/modules/opencuria-native-ui` Expo module to expose the platform AppCompat Toolbar. Native-stack screens require an activity React root and cannot be embedded inside React Native's Android dialog root. iOS modal headers use SwiftUI NavigationStack/Toolbar.

## Chat chrome

The chat follows the Claude app layout. The header has no title: the leading button opens the sidebar, the trailing group holds New chat and an overflow menu (switch workspace, rename and delete chat; the menu title names the workspace on iOS). The transcript scrolls beneath the transparent header; iOS 26 applies a soft scroll-edge blur and liquid-glass bar items, earlier iOS versions blur the bar, and Android fades the content under tonal buttons. Status banners float directly below the header and the transcript pads its first message by their height.

The composer shows the editor on top and one toolbar below it: attachments (`+`), a pill with the model and effort that opens the model sheet, and Send/Stop. The model sheet combines recent models (up to six, with "All models" and search), reasoning effort and agent (engine plus Build/Plan, as in the webapp). An existing chat keeps its engine, so only that engine's modes are offered.

The composer keeps its native text input mounted while it grows for drafts, skills or attachments. Native editing retains selection, dictation, undo, spell checking and IME composition; only explicit mention insertion moves the caret. Attachment/search/send results remain scoped to their owning chat. Sending clears the draft at once and restores it if the send fails.

## Keyboard

`ChatLayout` follows native messengers: the transcript keeps a fixed full-screen layout and scrolls beneath the header and the floating composer, while `react-native-keyboard-controller` shifts only its content. The composer dock rides on top of the keyboard frame by frame through natively driven `Animated` values; JavaScript and React renders never sit between a keyboard frame and the composer.

- Opening the keyboard lifts the transcript only when the reader is at the newest message; sending closes the keyboard and keeps following the reply.
- The transcript scrolls freely with the keyboard open. On iOS (`keyboardDismissMode="interactive"`) and Android 11+ (`KeyboardGestureArea`) the keyboard follows the finger as soon as a drag reaches the composer. On iPhone the dismiss area is extended to the composer through the composer input's `nativeID`; iPad keeps the system area because a minimized hardware keyboard can swallow the extender's events.
- Pulling the composer down closes the keyboard: on Android 11+ the keyboard follows the finger, on iOS a short, quick pull hands over to the system close animation. Holds (caret, selection), side swipes and scrolling drafts keep their native text gestures.
- On iOS 26 the composer is interactive liquid glass (`GlassView isInteractive`), unclipped so it can react to touches; “Scroll to latest” is a glass button floating above the composer.

The transcript retains unboxed assistant text, quiet user bubbles, native Markdown, media previews and small, uncolored copy/fork controls aligned with the text. It has no accent colors. Contiguous agent work collapses into one quiet activity line (“Explored 9 files, 2 searches”, “Thought”) between text, and the session to-dos render as an inline card where the agent last updated them. A lost live connection shows no banner; the app reconnects and refreshes on its own. User-message context menus are removed during editing to preserve native selection gestures. Question choices use native selection controls; unresolved question/permission gates remain non-dismissible.

## Sidebar

The sidebar follows the Claude app; it is not a separate screen. Opening it slides the chat right as a rounded, dimmed card and reveals the sidebar beneath it. A horizontal swipe anywhere on the chat opens it, a swipe to the left or a tap on the visible chat edge closes it, and Android Back closes it first. Vertical scrolling and held fingers (text selection, context menus) keep their own gestures; the keyboard closes as the sidebar opens.

Layout, top to bottom: the OpenCuria mark and name with a round search button, a Workspaces row (opens the workspace picker), the Inbox, and chats grouped by workspace. Workspace names are smaller section labels that collapse their group; the pen beside a name starts a chat in that workspace when it can take one. Chat rows are compact borderless rows with a status icon (chat, spinner, waiting gate), a single-line title and a quiet relative time (the workspace name in the Inbox). Long-press a chat for Rename, Mark as read/unread and Delete; the same actions are accessibility actions. An account button and a New chat pill float at the bottom.

Web and mobile import the same eligibility, sorting and inbox helpers from `packages/chat-core/src/conversationGroups.ts`. Scheduled tasks remain outside mobile.

Running and lifecycle-reserved workspaces are grouped, online first. Each group shows four newest chats by default, and Show more adds four independently. Opening an older active chat reveals its page. Closing navigation preserves group/page state; switching server or organization resets it. Stopped history remains reachable through All workspaces and eligible Inbox entries. Transitioning workspaces retain readable history with their mutation menus disabled.

## Workspace tools

Git, Terminal and Files of the chat's workspace open as one full-screen sheet: a UIKit full-screen presentation on iOS and a system dialog on Android. Pull up past the newest message to open it; the pull only arms when the finger lands while the transcript already rests at its end, so scrolling down to the end, momentum and reversing direction never open it. A quiet hint follows the pull and says "Release to open" once it would open; a quick flick opens with less travel. The chat's "…" menu offers the same sheet ("Git, Terminal & Files") for assistive technologies. The sheet needs a running workspace with an online runner.

The header holds Close leading and the sections trailing (native segmented control: SwiftUI picker / Material segmented buttons); Git opens first. Sections mount on first use and stay alive while the sheet is open. Viewers (diffs, commit details, files) open as page sheets above it with a native bar: close leading, title and subtitle, and an overflow menu.

- **Git** mirrors the webapp panel and shares its rules (`packages/chat-core/src/gitModel.ts`, `gitGraph.ts`, `gitRefGroups.ts`, `gitStash.ts`) and REST contract (`client.git`): repository menu and Fetch, branch with ahead/behind, commit message with the small-model generator, Commit / Commit & Push, the suggested remote action (Publish, Push, Pull, Sync) with its menu, the merge banner, Staged Changes and Changes with stage/unstage/discard (row buttons, long-press menu, confirmations), and the commit graph. The graph uses the webapp's vscode-git-graph layout; each row draws its own lane segments (cut at the row boundary) so the native list stays virtualized. Branch, remote and stash badges and long-pressed commits offer the webapp's actions. Tapping a change or a commit file opens a unified diff (jsdiff); a commit opens its details. Summaries poll every 4 s and details every 15 s only while Git is visible.
- **Terminal** uses t3code's native terminal surface (libghostty with Metal on iOS, libghostty-vt with a Canvas renderer on Android) on OpenCuria's terminal protocol: REST start plus Socket.IO input/resize/output, base64 bytes decoded as a UTF-8 stream. A key row rides above the keyboard (arrows, esc, ctrl/alt, tab, paste, clear, ~ | / -); the panel menu changes text size, clears, restarts, and adds the visible output to the chat. The shell outlives the sheet (bounded retained output is replayed) and restarts after a socket reconnect.
- **Files** follows t3code's browser: a search field (runner file search), the lazily listed `/workspace` tree with the webapp's file icons, and a viewer with native syntax highlighting (@pierre/highlights lexers compiled to C), Markdown, CSV/TSV table, SVG and image previews, Preview/Source, word wrap, copy path/contents, share and "Mention in chat".

## Verification

Run from the repository root:

```sh
npm run type-check
npm run test -w mobile
npm run test:shared
npm run export -w mobile
```

Native visual checks use the actual Android development client with disposable local API/Socket.IO sample data. These checks do not start external provider runs. Android/iOS Hermes export verifies JavaScript bundles; an iOS native build and iPhone/simulator acceptance require macOS/Xcode.

See [Native UI verification](mobile-native-verification.md) for checked flows and screenshots.
