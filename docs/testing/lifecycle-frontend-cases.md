# Frontend lifecycle verification cases

Recorded before new execution on 2026-10-31. The 82 cases below supplement original U001–U008 in [the original frozen plan](lifecycle-evidence/plan-before-execution.md). Expectations are recorded before running tests. PASS requires the final named test to execute; these are automated DOM/store/pure-selector tests, not physical hardware or real-browser tests. The exhaustive state matrix covers all 8 canonical workspace states × 2 runner connection states × 2 automatic-recovery states. Operation overlays, image/build states, and resource boundaries are listed separately.

Recovery means the existing `intervention_required` protocol flag while automatic reconciliation checks an interrupted operation. It does not imply administrator acknowledgment. Reset cases assume an available base image. Capture cases assume credentials have been scrubbed. Offline removal is a durable queued backend operation; offline create/start/capture/reset remain unavailable.

| ID | Scenario → expected result | Verification | Status |
|---|---|---|---|
| F001 | creating; runner online; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F002 | creating; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F003 | creating; runner offline; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F004 | creating; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F005 | running; runner online; recovery clear → stop, edit, capture, reset, remove enabled; chat ready. | `workspaceLifecycle.test.ts` | PASS |
| F006 | running; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F007 | running; runner offline; recovery clear → remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F008 | running; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F009 | stopped; runner online; recovery clear → start, edit, capture, reset, remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F010 | stopped; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F011 | stopped; runner offline; recovery clear → remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F012 | stopped; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F013 | failed; runner online; recovery clear → edit, reset, remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F014 | failed; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F015 | failed; runner offline; recovery clear → remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F016 | failed; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F017 | pending_deletion; runner online; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F018 | pending_deletion; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F019 | pending_deletion; runner offline; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F020 | pending_deletion; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F021 | deleting; runner online; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F022 | deleting; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F023 | deleting; runner offline; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F024 | deleting; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F025 | deleted; runner online; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F026 | deleted; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F027 | deleted; runner offline; recovery clear → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F028 | deleted; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F029 | delete_failed; runner online; recovery clear → remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F030 | delete_failed; runner online; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F031 | delete_failed; runner offline; recovery clear → remove enabled; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F032 | delete_failed; runner offline; recovery pending → all mutations blocked; chat blocked. | `workspaceLifecycle.test.ts` | PASS |
| F033 | Running workspace; creating active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F034 | Running workspace; starting active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F035 | Running workspace; stopping active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F036 | Running workspace; restarting active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F037 | Running workspace; removing active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F038 | Running workspace; capturing_image active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F039 | Running workspace; resetting active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F040 | Running workspace; updating active → all mutations and chat blocked; operation label retained. | `workspaceLifecycle.test.ts` | PASS |
| F041 | Image build absent; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F042 | Image build absent; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F043 | Image build pending; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F044 | Image build pending; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F045 | Image build building; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F046 | Image build building; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F047 | Image build active; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F048 | Image build active; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F049 | Image build deactivated; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F050 | Image build deactivated; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F051 | Image build failed; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F052 | Image build failed; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F053 | Image build pending_deletion; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F054 | Image build pending_deletion; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F055 | Image build deleting; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F056 | Image build deleting; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F057 | Image build delete_failed; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F058 | Image build delete_failed; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F059 | Image build deleted; runner online → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F060 | Image build deleted; runner offline → correct status/actions; locked definition blocks mutations; polling only for in-flight states. | `imageDefinitionLifecycle.test.ts` | PASS |
| F061 | Workspace settings vCPU below minimum → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F062 | Workspace settings vCPU above maximum → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F063 | Workspace settings fractional vCPU → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F064 | Workspace settings NaN vCPU → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F065 | Workspace settings RAM below minimum → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F066 | Workspace settings RAM above maximum → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F067 | Workspace settings fractional RAM → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F068 | Workspace settings disk below minimum → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F069 | Workspace settings disk above maximum → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F070 | Workspace settings fractional disk → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F071 | Workspace settings odd desktop width → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F072 | Workspace settings odd desktop height → no PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F073 | Workspace settings resource lower bounds → one PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F074 | Workspace settings resource upper bounds → one PATCH; validation precedes any restart request. | `EditWorkspaceDialog.test.ts` | PASS |
| F075 | Failed workspace deletion → retry removal available to ordinary user. | `WorkspaceActions.test.ts` | PASS |
| F076 | Runner offline → workspace removal remains available and queues server-side. | `WorkspaceActions.test.ts` | PASS |
| F077 | Deleted workspace → no action controls or misleading Working spinner. | `WorkspaceActions.test.ts` | PASS |
| F078 | Interrupted offline workspace → reconnect recovery message; no administrator required. | `WorkspaceActions.test.ts` | PASS |
| F079 | Clean stopped workspace in chat header → capture enabled without requiring running chat. | `WorkspaceChatHeader.test.ts` | PASS |
| F080 | Runner disconnects during operation → header and card prominently report Runner offline. | `WorkspaceChatHeader.test.ts + WorkspacesView.test.ts` | PASS |
| F081 | Failed/stopped header state → no green running indicator. | `WorkspaceChatHeader.test.ts` | PASS |
| F082 | Creating/deleting state without active-operation event → store keeps actions blocked and supplies state label. | `workspaces.test.ts` | PASS |

## Execution evidence

- F001–F082: **PASS**. Final focused coverage: `targeted-complete.xml` (212 tests, 13 files) plus `queue-final.xml` (64 tests, 2 files after the final queued-state label change).
- Final frozen-code full webapp suite: **PASS**, 2,247 tests / 211 files (`full-suite-green.xml`, exit 0, 327.83 seconds). Every planned F001–F082 case is linked to individually named passing JUnit assertions in `case-evidence-map.json`. Exact executed assertion names are retained in the portable map.
- Superseded runs remain visible: `full-suite.xml` passed 2,244 tests before the last sidebar/queue additions; `full-suite-final.xml` caught one obsolete assertion expecting “Stopped” for an explicitly offline runner. Only that test expectation changed, then `sidebar-final.xml` passed all 24 sidebar tests and the final full run above passed. Initial failed targeted/build/browser-fixture runs are retained alongside their successful replacements.
- Production build and Vue/TypeScript check: **PASS**, `npm run build` (`build-verified.log`, exit 0); final test-only expectation edit also passed `npm run type-check` (`typecheck-verified.log`, exit 0). Existing Vite glob-deprecation and large-chunk notices remain.
- ESLint and Oxlint: **PASS**, 19 touched files, zero errors/warnings (`eslint.log`, `oxlint.log`); final queue/store and sidebar-test edits also passed ESLint (`eslint-queue-final.log`, `eslint-sidebar-final.log`). `git diff --check` passed.
- Headed Chromium smoke against the final production build: **PASS**, 16 assertions (`browser-smoke.json`). Ordinary-member retry, offline removal queue, online/offline automatic recovery, stopped capture, hidden deleted rows, and offline sidebar/card labels verified. No page errors. Every API mutation was intercepted in an isolated localhost fixture; the page's external font reference was blocked locally.
- Screenshots: `workspace-states.png`, `offline-recovery.png`, `offline-removal-queued.png`. Browser context closed and preview server stopped after verification.
- Portable [frontend evidence](lifecycle-evidence/frontend/checks.json), [named assertion map](lifecycle-evidence/frontend/case-evidence-map.json), and [final JUnit](lifecycle-evidence/frontend/full-suite-green.xml) are included. Raw logs and earlier failed runs remain workspace-local. Workspace disk had 17 GiB free at the final frontend check.
- Browser testing extends F080 to the sidebar and verifies that an offline operation never displays a live-activity spinner. F076 also verifies explicit “Removal queued” labels and reconnect-aware notification.
- These are frontend selector/store/DOM and mocked-local-API browser tests. They do not claim real backend reconnect, physical hardware faults, or real VM image execution; those belong to the separately documented backend/runner/VM suites.
- Original U007's image-specific disk floor is enforced and verified in the backend/real-VM suites. Frontend image metadata does not expose that floor; reset-dialog tests cover readiness/version selection, and F061–F074 cover client resource bounds. These are distinct claims.
