# Native chat UI contracts

Presentation only: these components depend on `@opencuria/chat-core` for serialized protocol types and deterministic helpers. The app owns navigation, transport, request lifecycle, media permissions, workspace access, persistence, and settings. Components do not instantiate clients, sockets, or services. Copy is English. Controls expose accessibility roles/states/labels and stable `testID`s.

## Theme and primitives

Wrap the chat root in `<ThemeProvider initialMode="system" | "light" | "dark">`; `useTheme()` returns `{ mode, setMode, theme }`. Theme mode stays provider state so a host app may persist preference. Colors are a small semantic set in `src/ui/theme.tsx`, neutral surfaces, contrasting send actions, and separate prompt/composer surfaces. OpenCuria branding retains its own brand color.

`Button`, `IconButton`, `NativeAction` and `Banner` are exported by `src/ui/primitives.tsx`. Interactive controls use platform implementations: SwiftUI buttons on iOS, Material buttons/surfaces on Android. The OS owns highlighting, disabled appearance and hit testing. `NativeModal` uses system modal presentation with a native navigation bar, without a hand-drawn sheet handle, backdrop or gesture. `NativeList`, `NativePicker`, `NativeOption` and `NativeTextField` map directly to platform controls. `Drawer` slides the chat aside to reveal the sidebar (`SlideDrawer`, gesture and spring on the UI thread) on iPhone and Android and keeps the adaptive split view on iPad. `NativeContextMenu` adds a long-press menu to tappable content.

`WorkspaceSidebar` renders compact Claude-style rows with shared `conversationGroups` rules and owns the sidebar header, search, account button and New chat pill. Its per-workspace pagination and collapse state are retained while navigation is closed and cleared when the organization/server scope changes. Each workspace starts with four chats; an older active chat reveals its containing page.

## Transcript

```tsx
<ChatMessages
  messages={messages}
  sessions={sessions}
  todos={todos}
  busy={session.status === "busy"}
  streamingMessageId={streamingMessageId}
  resolveMedia={(path) => nativeMediaCache.resolvePinned(path)}
  onRequestPartDetail={(sessionId, partId) => api.getPart(sessionId, partId)}
  onOpenFile={(path) => navigation.openFile(path)}
  onOpenAttachment={navigation.openAttachment}
  onOpenSubtask={(childSessionId) => navigation.openSession(childSessionId)}
  onEdit={(messageId, text) => controller.edit(messageId, text)}
  onFork={(messageId) => controller.fork(messageId)}
/>
```

`messages: HarnessMessage[]` are ordered by persisted position, and each message's parts by their per-message position. `sessions` feed the live-run label. Busy and optional `streamingMessageId` anchor live state. User prompts use a subdued bubble; assistant text stays unboxed and the transcript carries no accent colors (links are underlined, actions are quiet). The shared `harnessActivity` builder controls chronology and grouping: every contiguous run of work (tools including edits and failures, patches, reasoning, subtasks) is one quiet line such as “Explored 9 files, 2 searches”, split only by text, steers, answered questions and the to-do card. Unlike the webapp, finished turns are not folded into a “Worked for” card. `todos` render as a To-dos card in place of the latest `todowrite` call, or after the latest reply for engines that publish plans without one; finished lists fold to their header. Expanded groups mount one `HARNESS_PAGE_SIZE` page at a time. Heavy detail fetches only on expansion when `detail_loaded === false`; the callback may return `HarnessPart | void`.

`resolveMedia` accepts a workspace path and returns a host-resolved local native URI or `{ uri, release }` lease. Workspace images and `@file:` image mentions use `expo-image` and a native full-screen sheet. Tool image/PDF attachments have a visible card; `onOpenAttachment` delegates data-URL viewing to the host. Never attach auth headers to external fetches. Markdown is rendered with native `react-native-enriched-markdown` in GFM mode; Marked's lexer identifies image tokens without mistaking fenced/code spans. Only credential-free `http:` / `https:` external links open; workspace links call `onOpenFile`. No WebView or Streamdown.

User message `@file:`/`@agent:` context tokens are parsed by the shared `composerTokens` helper and rendered in-place as ordered native chips/media, not raw token strings. Copy uses Expo Clipboard and canonical `harnessMessageCopy`; `onCopy` is optional. Long-press a user bubble for native copy and edit/rerun menu actions. Editing removes the menu wrapper so native text selection owns its gestures. Assistant copy/fork controls sit below completed replies; their details menu holds model, timestamp and usage. Edit/fork controls appear only when callbacks are provided and actions are enabled. Messages are ordered by the shared `sortHarnessMessages`, so a live reply without a server position stays last. The transcript is a `KeyboardAwareLegendList` that opens at the newest message and follows new and streaming output while the reader stays at the end; a floating “Scroll to latest” button appears once the reader moves away. Host handlers may be inline: the transcript keeps its rows stable while a reply streams.

## Keyboard layout

```tsx
<ChatLayout composer={<ChatComposer … />} overlay={<StatusBanners />}>
  <ChatMessages … />
</ChatLayout>
```

`ChatLayout` owns the keyboard choreography. The transcript fills the screen; the composer floats in a dock that follows the keyboard through natively driven `Animated` values. `useChatLayout()` shares the dock geometry with the transcript (its end inset, the jump button and the empty state) and `dismissKeyboard({ revealLatest })`, which closes the keyboard and then scrolls to the newest message. See [Native chat design](../../../docs/mobile-chat-design.md#keyboard) for the gestures.

## Composer

```tsx
<ChatComposer
  draft={draft}
  workspaceName={isNewChat ? workspace.name : undefined}
  onDraftChange={(text, selection) => setDraft(text, selection)}
  onSend={({ text, skillIds, attachments }) =>
    controller.send({ text, skillIds, attachments })
  }
  busy={busy}
  onStop={() => controller.abort()}
  contextKey={workspaceId + ":" + sessionId}
  settings={{
    harness: "OpenCuria",
    mode: "build",
    model: "Model",
    effort: "High",
  }}
  onSettings={() => navigation.openModelSettings()}
  skills={skills}
  selectedSkillIds={skillIds}
  onSelectedSkillIdsChange={setSkillIds}
  onSearchMentions={(query) => workspace.findMentionCandidates(query)}
  proposals={mentionCandidates}
  onTakePhoto={pickers.capturePhoto}
  onChoosePhotos={pickers.choosePhotos}
  onChooseFiles={pickers.chooseDocuments}
  attachments={attachments}
  onAttachmentsChange={setAttachments}
/>
```

Draft is controlled. Native editing does not apply a stale controlled cursor; `onDraftChange` receives a cursor estimate which the next `onSelectionChange` corrects. Only `@` / `/` proposal insertion explicitly moves the caret. Parent prefill/reset clears proposals. `contextKey` invalidates outstanding picker/search/send results when the owning workspace/session changes. Mention search is debounced (150ms) and generation-guarded; late picker results are discarded. Async send, stop, and picker failures render an inline error; failed sends preserve the draft.

`onSend` receives `{ text, skillIds, attachments }`; the composer clears the draft as soon as it sends and restores it when `onSend` rejects. Uploaded-file token conversion and attachment reset remain parent responsibilities. Busy always means stop, including with empty draft. Unknown selected skill IDs remain visible rather than silently disappearing. The model and effort label opens a host-owned picker and exposes its summary to accessibility services. The optional `workspaceName` labels new chats above the editor. Send/stop uses a 36-point contrasting circle within a 44-point native touch target. Attachments open an anchored system menu. `ComposerSurface` uses interactive native iOS liquid glass where available and a Material surface on Android. `ComposerEditor` uses the OS `UITextView` / `EditText` through React Native’s multiline input, matching t3code’s underlying editor primitives. The editor sits above one toolbar row and grows with the draft up to about seven lines before it scrolls; its `nativeID` links it to the keyboard's interactive-dismiss area. Pulling the composer down closes the keyboard (`ComposerDismissArea`). Core `MentionCandidate[]` drive optional suggestions: file selection inserts `@file:/workspace/...`, agent inserts `@agent:name`, skill adds an ID without sending.

Attachment adapters own permissions, Expo ImagePicker / DocumentPicker, conversion/upload, and any failure feedback. Undefined callbacks hide unavailable actions. Image attachments show local thumbnails. For attachment-only sends the parent defines the harness upload/reference behavior.

## Gates

```tsx
<ChatGates
  permissions={permissionRequests}
  questions={questionRequests}
  onResolvePermission={controller.resolvePermission}
  onSubmitQuestion={controller.resolveQuestion}
  onSkipQuestion={controller.skipQuestion}
/>
```

`ChatGates` mounts exactly one system modal; permission takes priority when both request types are pending. `priority="question"` lets the parent choose otherwise. Individual `PermissionGate` and `QuestionGate` are exported for hosts that already guarantee only one gate is visible. Each supports paging when multiple requests of its type are queued. Permissions are `once | always | reject`. Question answers are `string[]`; multi-select uses the protocol NUL separator (`\0`) and may include a free-text value. Errors remain in the sheet and can be retried; unresolved gates are non-dismissible.

## Checks

Focused tests in `ChatComposer.test.tsx`, `ChatMessages.test.tsx`, `ChatMarkdown.test.tsx`, and `ChatGates.test.tsx` mock the native Markdown renderer / LegendList at test boundary. Run them with `npm run test -w mobile` from the repository root; `npm run type-check -w mobile` checks the native client.

Public component exports live in `mobile/src/components/index.ts`; UI/theme exports in `mobile/src/ui/index.ts`.
