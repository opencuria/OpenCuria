# Native chat UI contracts

Presentation only: these components depend on `@opencuria/chat-core` for serialized protocol types and deterministic helpers. The app owns navigation, transport, request lifecycle, media permissions, workspace access, persistence, and settings. Components do not instantiate clients, sockets, or services. Copy is English. Controls expose accessibility roles/states/labels and stable `testID`s.

## Theme and primitives

Wrap the chat root in `<ThemeProvider initialMode="system" | "light" | "dark">`; `useTheme()` returns `{ mode, setMode, theme }`. Theme mode stays provider state so a host app may persist preference. Colors are a small semantic set in `src/ui/theme.tsx`, neutral surfaces with OpenCuria violet for action emphasis.

`Button`, `IconButton`, `Sheet` and `Banner` in `src/ui/primitives.tsx` provide native touch targets, safe-area-aware modal sheets and inline status feedback. Icon elements are supplied by the caller. Parent layout owns outer placement and insets.

## Transcript

```tsx
<ChatMessages
  messages={messages}
  sessions={sessions}
  todos={todos}
  busy={session.status === 'busy'}
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

`messages: HarnessMessage[]` are ordered by persisted position, and each message's parts by their per-message position. `sessions` / `todos` feed the live-run banner and task summary. Task details open in a native sheet. Busy and optional `streamingMessageId` anchor live state. User prompts use a subdued bubble; assistant text stays unboxed. The canonical `harnessBlocks` builder controls chronology and work grouping. Heavy detail fetches only on expansion when `detail_loaded === false`; the callback may return `HarnessPart | void`.

`resolveMedia` accepts a workspace path and returns a host-resolved local native URI or `{ uri, release }` lease. Workspace images and `@file:` image mentions use `expo-image` and a native full-screen sheet. Tool image/PDF attachments have a visible card; `onOpenAttachment` delegates data-URL viewing to the host. Never attach auth headers to external fetches. Markdown is rendered with native `react-native-enriched-markdown` in GFM mode; Marked's lexer identifies image tokens without mistaking fenced/code spans. Only credential-free `http:` / `https:` external links open; workspace links call `onOpenFile`. No WebView or Streamdown.

User message `@file:`/`@agent:` context tokens are parsed by the shared `composerTokens` helper and rendered in-place as ordered native chips/media, not raw token strings. Copy uses Expo Clipboard and canonical `harnessMessageCopy`; `onCopy` is optional. Edit/fork controls appear only when callbacks are provided and actions are enabled. A floating “Scroll to latest” button appears only after the reader moves away from the tail; LegendList keeps its visible-content anchor while scrolled up.

## Composer

```tsx
<ChatComposer
  draft={draft}
  onDraftChange={(text, selection) => setDraft(text, selection)}
  onSend={({ text, skillIds, attachments }) => controller.send({ text, skillIds, attachments })}
  busy={busy}
  onStop={() => controller.abort()}
  contextKey={workspaceId + ':' + sessionId}
  settings={{ harness: 'OpenCuria', mode: 'build', model: 'Model', effort: 'High' }}
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

`onSend` receives `{ text, skillIds, attachments }`; draft reset and uploaded-file token conversion remain parent responsibilities. Busy always means stop, including with empty draft. Unknown selected skill IDs remain visible rather than silently disappearing. Settings are a host-owned summary/picker. Core `MentionCandidate[]` drive optional suggestions: file selection inserts `@file:/workspace/...`, agent inserts `@agent:name`, skill adds an ID without sending.

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

`ChatGates` mounts exactly one native Modal; permission takes priority when both request types are pending. `priority="question"` lets the parent choose otherwise. Individual `PermissionGate` and `QuestionGate` are exported for hosts that already guarantee only one gate is visible. Each supports paging when multiple requests of its type are queued. Permissions are `once | always | reject`. Question answers are `string[]`; multi-select uses the protocol NUL separator (`\0`) and may include a free-text value. Errors remain in the sheet and can be retried; unresolved gates are non-dismissible.

## Checks

Focused tests in `ChatComposer.test.tsx`, `ChatMessages.test.tsx`, `ChatMarkdown.test.tsx`, and `ChatGates.test.tsx` mock the native Markdown renderer / LegendList at test boundary. Parent application is expected to run them under jest-expo and React Native Testing Library. This isolated repo currently has no mobile package manifest or RN runtime, so an app-level jest/typecheck cannot run here.

Public component exports live in `mobile/src/components/index.ts`; UI/theme exports in `mobile/src/ui/index.ts`.
