# Native platform adapters

Expo SDK 57 adapters for media, pickers, uploads and secure storage. They do not
own app config, permission copy or the auth lifecycle.

- `media.ts`: `MediaCache` for one workspace and server/auth/organization scope.
  `resolvePinned(path)` returns a URI plus `release()`; pinned files survive LRU
  eviction (32 MiB, previews up to 20 MiB). `dispose()` when the scope ends; never
  persist its URIs. Offline pictures live in `src/offline/`, not here.
- `pickers.ts`: `pickPhoto('camera' | 'library')` and `pickDocuments()` return
  `ComposerAttachment[]` (empty on cancel). HEIC and oversized photos become JPEGs
  up to 2048 px / 10 MiB. `uploadAttachments()` checks sizes first and never
  overwrites existing uploads.
- `storage.ts`: `NativeSessionStorage` keeps tokens, server URL and organization in
  one SecureStore record, never a password. `clear()` cannot be undone by an older save.
- `gitSelection.ts`: remembers the selected repository per server, organization and
  workspace.
