# Native syntax highlighter

Expo module `OpenCuriaHighlights` with one synchronous function,
`tokenize(source, languageId) -> ArrayBuffer | null`, returning compact
`(endUtf16, tokenId)` records per line (a token ID of `0xFFFFFFFF` ends a line).
`mobile/src/tools/files/highlights.ts` expands them into coloured tokens for the
file viewer. Builds without the module render plain text.

Adapted from t3code's `t3-highlights`; see `NOTICE.md` for the vendored
@pierre/highlights lexers (wasm2c output), the wasm2c runtime and SIMDe.
Expo autolinks local modules from `mobile/modules/`; rebuild the development
client after changes.
