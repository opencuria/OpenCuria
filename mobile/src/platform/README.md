# Native platform adapters

Expo SDK 57 adapters for media, pickers, uploads and secure storage. They do not
own app config, permission copy or the auth lifecycle.

- `media.ts`: `MediaCache` for one workspace and server/auth/organization scope,
  over the signed links of `client.files`. `describe(path)` gives size and blurred
  preview; `resolvePinned(path)` downloads an image (videos return their streaming
  URL) and returns a URI plus `release()`; pinned files survive LRU eviction
  (32 MiB). `openFile` downloads one document at a time. Every file is limited to
  500 MB. `dispose()` when the scope ends; never persist its URIs. Offline pictures
  live in `src/offline/`, not here.
- `pickers.ts`: `pickPhoto('camera' | 'library')` and `pickDocuments()` return
  `ComposerAttachment[]` (empty on cancel). HEIC photos become JPEGs up to 2048 px.
  `uploadAttachments()` checks sizes first, then streams each file from disk with
  one `PUT`; the server renames instead of overwriting.
- `storage.ts`: `NativeSessionStorage` keeps tokens, server URL and organization in
  one SecureStore record, never a password. `clear()` cannot be undone by an older save.
- `gitSelection.ts`: remembers the selected repository per server, organization and
  workspace.
