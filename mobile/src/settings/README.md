# Settings

Native settings in the style of ChatGPT's: a page sheet with a navigation
stack that covers every setting of the webapp.

- `SettingsScreen.tsx` presents the sheet, owns the page path and routes.
- `SettingsNavigator` is a SwiftUI `NavigationStack` on iOS (system push,
  swipe back, liquid-glass bar items) and a Material back stack on Android.
- `SettingsPage` is one page: a title, `NativeForm` sections, an optional
  primary action (iOS bar / Android button or FAB), overflow actions,
  search, loading and empty states. Pages never draw their own chrome.
- `pages/RootPage.tsx` is the first page: profile, organization card and
  grouped links with prominent section titles. Page groups:
  `pages/models` (Provider & Models, Agents, Skills), `pages/access`
  (Credentials, Credential services, Plugins, API keys) and
  `pages/infrastructure` (Captured images, Image definitions, Runners).

## Writing a page

- A page is a component that renders `<SettingsPage … />` with
  `NativeFormSection[]`. Use only `NativeForm` row kinds; add a row kind to
  `ui/NativeForm.*` (iOS, Android and `test/native-ui.cjs`) instead of
  drawing custom views.
- Navigate with `useSettingsNavigation()`: `link` rows push their `route`
  natively; `push`, `pop`, `replace`, `popToRoot`, `close` and `choose` (a
  pushed, searchable single choice) are for actions. `present` shows an
  existing sheet (e.g. the workspace capture sheet) above Settings, outside
  the native page stack.
- Routes are `"<page>"` or `"<page>:<argument>"` (`routes.ts`). Each page
  group resolves its own routes in `pages/<group>/index.tsx`.
- Read data with `useResource(resources, key, loader)`. Pages that show the
  same data (a list and the detail pushed above it) share the key, and a
  save updates it with `resource.set(response)`.
- Settings that the webapp saves automatically use `useAutosave` and show
  `autosaveSection(...)`; forms with secrets or several fields save with a
  primary action instead.
- Confirm destructive actions with `Alert.alert`, report results with
  `showToast`, show load failures with `loadErrorSections`. Deletions of
  images, builds and definitions go through `pages/DeletionPage`.
- Read-only text (keys, tokens, logs) uses `pages/TextPage` with Copy and
  Share; one-time secrets live only in the resource store of this opening.
- Poll with `usePolling` while a page is mounted; reload after the browser
  with `useForegroundReload` (MCP OAuth finishes in the webapp, because the
  server binds that flow to a browser session).
- Rules shared with the webapp live in `@opencuria/chat-core`
  (`harnessAgents`, `providerAccounts`, `pluginForms`, `plugins`,
  `imageDefinitionLifecycle`); pages only arrange them as rows.
- Members never see settings only admins may change: no links, no read-only
  copies, no admin hints. List such pages in `ADMIN_PAGES` (`routes.ts`) and
  hide their links when `useSettings().isAdmin` is false; the server
  enforces every rule again.
- Workspace names are private to their owners: the server sends them only to
  the owner, so show image usage as counts and other users' workspaces by
  owner.
- Test pages with `renderSettingsPage` and `mockClient` from `testUtils.tsx`.
