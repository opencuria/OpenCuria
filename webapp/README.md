# Webapp

Vue 3 + Vite dashboard for workspaces, chat, credentials and images. Use only the
shadcn-vue components in `src/components/ui/`.

Install dependencies once with `npm ci` at the repository root, then:

```sh
npm run dev:web             # from the repository root
npm run build -w webapp     # type-check and production build
npm run lint -w webapp
npm run test -w webapp
```

The Vue DevTools overlay is opt-in (`VITE_VUE_DEVTOOLS=true npm run dev:web`):
attached DevTools make Pinia deep-watch every store, which slows busy chats.

## Clipboard

Use `CopyButton` for buttons and `copyToClipboard` for menu actions. Call it
directly in the click or key handler, before unrelated awaits; for text loaded
asynchronously pass a producer (`() => Promise<string>`) so Safari keeps the user
gesture. The desktop viewer handles Cmd/Ctrl+C/V itself through the workspace
clipboard endpoint.
