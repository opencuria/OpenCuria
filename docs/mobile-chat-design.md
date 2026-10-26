# Native chat design

The native-control reference is `t3code/apps/mobile/` in the workspace. OpenCuria keeps its chat, workspace and account structure while using actual platform controls for interaction. Reference code is read-only; no t3code assets or business logic are copied.

## Platform mapping

| Element | iOS | Android |
| --- | --- | --- |
| Buttons | SwiftUI Button and SF Symbols | Material Button/IconButton |
| Rich transcript actions | SwiftUI Button | Material clickable Surface |
| Chat editor | Native multiline UITextView | Native multiline EditText |
| Composer surface | Native liquid glass where supported | Material Surface |
| Attachment and message menus | UIMenu | Android PopupMenu |
| Model, engine and effort selection | SwiftUI menu Picker | Material exposed dropdown |
| Question options | Standard checkmark selection rows | Material RadioButton/Checkbox rows |
| Workspace/chat lists | SwiftUI List/Section | Material LazyColumn/ListItem |
| Navigation | Native adaptive NavigationSplitView | Android DrawerLayout |
| Modal screens | UIKit page-sheet presentation | Android system modal dialog |
| Headers | UINavigationBar | Android Toolbar |
| Login fields | SwiftUI TextField/SecureField | Material OutlinedTextField |

`@expo/ui` bridges SwiftUI and Jetpack Compose primitives. It is used for native controls, not for bottom-sheet presentation. No Expo bottom sheet, JavaScript sheet gesture, simulated handle or custom drawer animation is used. `@react-native-menu/menu` supplies OS context menus, and `expo-glass-effect` supplies the native iOS composer surface. These dependencies require a new development-client build.

Android modal headers use the local `mobile/modules/opencuria-native-ui` Expo module to expose the platform AppCompat Toolbar. Native-stack screens require an activity React root and cannot be embedded inside React Native's Android dialog root. iOS modal headers use SwiftUI NavigationStack/Toolbar.

The composer keeps its native text input mounted while it expands for replies, focus, drafts, skills or attachments. Native editing retains selection, dictation, undo, spell checking and IME composition; only explicit mention insertion moves the caret. Attachment/search/send results remain scoped to their owning chat. The keyboard controller manages keyboard avoidance.

The transcript retains unboxed assistant text, quiet user bubbles, native Markdown, media previews, task details and copy/fork controls. User-message context menus are removed during editing to preserve native selection gestures. Question choices use native selection controls; unresolved question/permission gates remain non-dismissible.

## Sidebar

Web and mobile import the same eligibility, sorting and inbox helpers from `packages/chat-core/src/conversationGroups.ts`. The native sidebar includes New chat, search, Inbox, workspace chat groups, All workspaces and account access. Scheduled tasks remain outside mobile.

Running and lifecycle-reserved workspaces are grouped, online first. Each group shows four newest chats by default, and Show more adds four independently. Opening an older active chat reveals its page. Closing navigation preserves group/page state; switching server or organization resets it. Stopped history remains reachable through All workspaces and eligible Inbox entries. Transitioning workspaces retain readable history with their mutation menus disabled.

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
