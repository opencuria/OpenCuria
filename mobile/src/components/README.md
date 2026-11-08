# Native chat UI

Presentation only. Components use `@opencuria/chat-core` types and helpers; the app
owns navigation, transport, permissions, persistence and settings. Components never
create clients, sockets or services. Copy is English; controls have accessibility
labels and stable `testID`s.

## Building blocks

- `src/ui/theme.tsx`: `ThemeProvider` / `useTheme()` with a small semantic palette.
- `src/ui/`: buttons (`primitives.tsx`), `Native*` lists, forms, text fields and
  modals, `ContextMenu` and `Drawer`, mapped to SwiftUI on iOS and Material on
  Android. Use these instead of drawing custom controls.
- `ChatLayout`: keyboard choreography; the composer dock follows the keyboard.
- `ChatMessages`: the transcript. Work between texts is grouped by the shared
  `harnessActivity` builder; finished turns fold into “Worked for”. Part details load
  only on expansion.
- `ChatComposer`: controlled draft. It clears on send and restores the draft when
  `onSend` rejects; `contextKey` discards late picker, search and send results.
  It hosts the sheet stack; `gates` passes pending questions and permissions.
- `ComposerSheets`: the sheet stack behind the composer top, as in the webapp (shared
  `composerSheets` order: mentions, questions, permissions). Only the top sheet is
  interactive; gate sheets fold into their header and shrink with the keyboard.
- `WorkspaceSidebar`, `MediaViewer`: sidebar with shared `conversationGroups` rules and
  the full-screen picture gallery.

## Rules

- `resolveMedia` returns a local URI or `{ uri, release }` lease; release it when hidden.
- Never attach auth headers to external fetches; only credential-free `http(s)` links
  open externally, workspace links go through `onOpenFile`.
- Undefined callbacks hide their actions.

Public exports: `index.ts`. Tests: `npm run test -w mobile` from the repository root.
