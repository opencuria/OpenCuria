# Native Expo platform adapters

These framework-neutral adapters target Expo SDK 57. They depend on the host
installing the corresponding Expo modules; this UI package intentionally does
not own application config, permissions copy, auth lifecycle, or dependency
versions.

## Media (`media.ts`)

```ts
const media = new MediaCache(client, workspaceId, authOrganizationScope)
const uri = await media.resolve('/workspace/output.png') // compatibility API
const visible = await media.resolvePinned('/workspace/output.png') // hold while rendered
// On unmount/when no longer visible:
visible.release()
const file = await media.openFile('/workspace/README.md') // { uri, mimeType, name, kind, text? }
await media.shareAttachment(toolAttachment.url) // bounded image/PDF data URL -> native share sheet
await media.clear() // clear files and invalidate in-flight ownership; adapter can be reused
await media.dispose() // permanent logout/workspace teardown
```

Use the same adapter instance only for its constructor's workspace and
server/auth/organization scope. Dispose it when that scope ends; do not persist
its URIs or use local media as offline chat history. Paths are validated with
`resolveWorkspaceLinkPath`; workspace previews/text reads are limited to 20
MiB, and document downloads are explicit-only (runner limit: 100 MiB). Preview
cache is app-private, LRU-capped at 32 MiB with two concurrent transfers. Use
`resolvePinned`/`pin` for visible media and call its idempotent `release()` when
that view no longer uses the URI; pins are protected from LRU eviction. The
unique cache directory and filenames contain no workspace/account identifiers.
`openFile` rejects oversize files with a user-displayable error code/message.

## Pickers and upload (`pickers.ts`)

`pickPhoto('camera' | 'library')` and `pickDocuments()` return
`ComposerAttachment[]`; cancellation returns an empty array. Picker permission
errors have friendly messages. DocumentPicker copies selected files to Expo's
cache, so `content://` inputs are immediately readable. HEIC/HEIF photos are
converted to JPEGs no wider than 2048 pixels and no larger than 10 MiB;
oversize raster photos are resized as well. Other documents are never
recompressed. Call `clearPickedPhotoCache()` when the picker session is abandoned
or after upload to delete only adapter-created converted image files.

`uploadAttachments(client, workspaceId, attachments)` validates local sizes
before reading base64, lists the shared chat-upload directory before choosing
names, and uploads through the client's authenticated chunked file transport.
It only treats an explicit missing-directory response as an empty listing;
all other list errors fail without uploading/overwriting.

## Secure auth session (`storage.ts`)

`NativeSessionStorage` serializes a single SecureStore record of access/refresh
tokens, server URL, and organization ID. It never stores a password. SecureStore
failures propagate. `clear()` immediately invalidates pending reads/saves and
queues deletion after any active write, so logout cannot be undone by an older
save.
