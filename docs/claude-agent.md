# Claude Agent

Claude Agent is a separate harness: the backend uses the pinned Python SDK and
workspace transport; the pinned Claude Code CLI runs as a managed process in
the selected Linux workspace.

## Connect and chat

In **Settings → Provider & Models → Claude Agent → Manage**, add a named
account, choose **API token** or **Subscription token**, and paste an authorized
token. You may keep multiple personal accounts; org admins may share accounts.
Select the account that new responses should use. There
is no Anthropic browser sign-in or managed OAuth flow. OpenCuria stores the
token using its encrypted credential store; the UI and APIs return
connection metadata, not the token. OpenCuria does not refresh tokens or manage
their expiry; to reconnect, paste a valid token.

In a new chat, choose **Claude Agent Build** or **Claude Agent Plan**. Chats keep
their selected harness; start a new chat to choose another. Model and effort use
the shared model picker, which offers `sonnet`, `opus`, and `haiku`. These are
runtime-resolved aliases, not fixed model versions. The pinned CLI resolves the
first-party aliases to the 5.5 family (including Haiku 5.5 since CLI 2.1.293).
The catalog advertises their current first-party defaults: 1M context and 128K
maximum output tokens; account entitlements and upstream policy still apply.
Both modes use the same configured permission rules as the native OpenCuria
harness. Build allows tools
by default; the primary Plan agent asks before direct file edits/writes but
allows Bash, background processes, and registered MCP tools without additional
Claude-specific approval gates. For native tools in Plan, hooks route through
the paired host callback, which rechecks OpenCuria policy; an allowed action
needs no extra user approval or Claude auto-mode classifier call. Explicit
`ask`/`deny` rules still apply.
Subagents use their own OpenCuria agent rules; general subagents are not
restricted by an extra Plan gate, while Explore remains read-only. Plan does not
switch to Build automatically.

The SDK/CLI retain their `plan`/`default` modes and route tool decisions through
OpenCuria hooks and its permission callback; `bypassPermissions` and blanket
auto-approval are not enabled. This is for trusted workspaces, not a shell
sandbox: Plan permission gates do not prevent file changes made through an
allowed shell command, process, or MCP tool.

API tokens are passed as `ANTHROPIC_API_KEY`; subscription tokens as
`CLAUDE_CODE_OAUTH_TOKEN`. Accounts are personal within the active organization
or shared by an org admin.
The current selection is captured per answer; a follow-up can switch accounts
without changing an answer already running. Deleting or unsharing a selected
account cancels affected answers without falling back. See
[provider accounts](provider-accounts.md) for scope, selection and API details.
Scheduled tasks resolve the owner's current selected account at each run and
never copy its token into a schedule or occurrence snapshot. Existing schedules default to the
native harness. Usage and billing follow the account for the token you provide;
CLI-reported cost is usage information, not an Anthropic invoice. See Anthropic's
[Claude Code legal and compliance information](https://docs.anthropic.com/en/docs/claude-code/legal-and-compliance).

## Native prompt and tools

Claude Code retains its native system-prompt preset. OpenCuria appends only a
small [shared product context](harness-prompts.md): workspace isolation and external
boundaries, permitted local setup, and Markdown file/media rendering. It adds no
coding persona, role policy, tool descriptions, project files, or skill bodies.
Context loading remains CLI-owned under the setting sources described below;
selected skills use the existing managed native plugin. Native shell/file/delegation
and the existing OpenCuria MCP/process/question/todo integration are unchanged.

## Messages while the agent runs

You can keep typing while a run is active (native and Claude Agent chats alike).
The message is queued above the composer and joins the running turn at its next
step: native runs add it after the current tool results, and Claude Agent
streams it to the CLI as a `next`-priority user message, which the CLI folds
into the turn after the current tool call. Delivered messages appear inline in
the transcript where the agent received them. If the turn ends first, the queued
message starts a follow-up run automatically; **Stop** discards the queue.
`POST /harness/sessions/{id}/steer` exposes this behavior.

## Runtime and updates

Deploy matching OpenCuria backend and runner versions; apply migrations
`0028_claude_harness`, `0029_harnessrun`, and `0006_scheduledtask_harness_id`.
The backend pins `claude-agent-sdk==0.2.174`; the runner provisions Claude Code
CLI `2.1.310` at:

```text
/opt/opencuria/runtimes/claude-agent/2.1.310/claude
```

The checked-in [artifact manifest](../runner/src/runtime/artifact_manifest.json)
and runner provisioner pin upstream release-manifest and signature-file
digests, platform binary sizes and SHA-256 values, and signing fingerprint
`31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`. Provisioning checks downloaded
binaries against compiled-in size/hash pins and the reported version; it does
not fetch and verify a live upstream signature. The fingerprint is trust-review
metadata. Four Linux variants are pinned: x64 and arm64 with glibc or musl;
unsupported platforms fail provisioning. Workspace images need `python3`, `sh`,
`sha256sum`, and `bash` for process control, artifact provisioning, and the CLI's
Bash tool.

The runner provisions the pinned CLI on demand and reuses the verified workspace
artifact; it does not look up `latest` or allow CLI self-updates. A version change requires a reviewed OpenCuria manifest/code
update and coordinated backend/runner deployment. Keep `$RUNNER_STATE_DIR`
persistent across restarts; the artifact cache is under
`artifacts/claude-agent/2.1.310/` in the runner state volume.

### Upgrade review: CLI 2.1.292 → 2.1.310 / SDK 0.2.164 → 0.2.174

Reviewed against the official [CLI changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
and [Python SDK changelog](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md)
on 2026-10-24:

| Change | OpenCuria handling |
| --- | --- |
| Plan hook approvals now keep CLI write checks (2.1.299) | Native Plan actions explicitly route to the existing host permission callback. Root edit approval, child scopes, explicit denies and MCP approval correlation remain enforced. No bypass/auto permission mode is enabled. |
| Fork subagents default on in SDK/headless sessions (2.1.302) | Disabled with the required fixed `CLAUDE_CODE_FORK_SUBAGENT=0` on both backend and runner/guest. Task/Agent hooks reject unsupported child types before launch, rather than inheriting the parent's tools. Scoped General/Explore delegation remains available. Native forks need explicit identity, permissions, depth and lifecycle support before being enabled. |
| Monitor and plugin monitors work with telemetry disabled (2.1.310) | Not exposed by the explicit native tool allowlist. Unknown tools fail closed; recognized native tools are also checked against the current agent's allowlist at the hook boundary. Use OpenCuria's managed process tools. Monitor support would need permission mapping, event projection and cleanup/lease integration first. |
| Haiku 5.5 becomes the first-party `haiku` alias (2.1.293) | Aliases still pass through; the catalog's outdated 200K/64K metadata is updated to the current 1M/128K first-party defaults. Alias targets and Anthropic pricing can change. |
| `Read.allow_large` (2.1.296), typed hook/init payloads (SDK 0.2.166) | Native schemas remain CLI-owned; no duplicated schemas or new UI required. Existing hook/event normalization remains compatible. |
| Tool allowlist enforcement, MCP reconnect/retry and compaction/resume fixes (2.1.295–2.1.310) | Adopted through the pinned update. Real-CLI contracts cover schemas, permissions, resume, child identity, media, steering, skills and todos. Encrypted eager transcript mirroring remains authoritative. SDK 0.2.170 also fixes parallel branches in its session-message helpers; OpenCuria restores raw mirrored entries. |
| Idle compaction (2.1.299) | Runs remain turn-scoped; native compaction and resume stay CLI-owned. No independent idle Claude process or OpenCuria scheduler is added. |
| Unanswered headless MCP elicitation (2.1.310) | The bridge has no generic MCP elicitation-to-UI handler. Use the existing OpenCuria question tool; interactive MCP elicitation needs a separate validated, cancellable user-input bridge before being offered. |
| Mods/plugin monitors, auto-mode setup, workflows, telemetry events, Remote Control/cloud/terminal UI changes | No automatic opt-in or duplicate OpenCuria UI. Selected skill bodies are trusted native plugin content and can contain executable frontmatter; review them like code. Supporting untrusted executable skill hooks/mods needs a separate policy review. Repository/user setting sources remain disabled. |

## Workspace and data

The runner launches the CLI with a minimal `HOME` and `PATH`, the explicit
harness environment, and the selected account credential. Ambient runner
credentials are not forwarded. OpenCuria passes the credential through the CLI process
environment, not its generated config files, and removes the isolated temporary
home after confirmed process cleanup.

This is for trusted workspaces, not an OS-level secret-isolation boundary. The
credential is available to the CLI process; workspace commands with sufficient
process access, including same-user processes, may inspect its environment.
Workspace administrators and runner-host root can inspect a live process or its
environment too.

The CLI uses strict run-configured MCP settings and does not load user or
repository setting sources. OpenCuria tools, permissions, MCP integrations, and
their credentials use OpenCuria's existing bridge. Selected skills are made
available through a generated Claude plugin in managed workspace state, not
inlined again in the system-prompt extension. Repository hooks are not automatically
enabled; OpenCuria does not create or edit a repository `CLAUDE.md`. Native tool
schemas and descriptions come from the pinned CLI and the MCP bridge, not a
duplicated tool list in the system-prompt extension. Native WebFetch/WebSearch use Claude's behavior,
including for Explore, rather than OpenCuria Webfetch's URL protections.

Each run, including resume, appends the same shared product context.
Scoped SDK subagents receive the same context in their explicit child prompts;
Explore omits local setup guidance. These child prompts are separate from upstream
subagent role prompts; see [integration boundaries](harness-prompts.md#separate-runtime-integration-boundaries).
Project-context discovery is left to the CLI rather than duplicated by OpenCuria.
Explicit user image/PDF references (`@file:/workspace/...`
and Markdown images) become bounded media inputs after read authorization;
workspace-media reads reject symlinks. Assistant/tool text never triggers these
implicit file reads. Other file references remain text for an explicit Read call.

Generated settings, prompts, selected skills, and restored CLI transcript files
live under `/workspace/.opencuria/harness/claude/` with private file/directory
modes. Prompts and transcripts may contain sensitive data; these files may
remain in the workspace and may be included in an image capture. The backend
stores the SDK transcript mirror as encrypted opaque entries using OpenCuria's
configured application encryption. Ordinary chat messages and workspace files
use their existing storage and access rules.

Only root assistant text is emitted in the root assistant's live text stream.
Subagent text is persisted in its child-session transcript and summarized in the
task display, not emitted as root text deltas. Resume uses the session's explicit
upstream UUID and stored transcript. Fork starts a fresh upstream transcript
with the copied OpenCuria message prefix; editing clears the stale transcript
and starts fresh. Neither action rolls back workspace files. Deleting a chat
removes transcript records through the session cascade. OpenCuria does not
transparently switch harnesses or replay Claude state in another harness. Missing
or oversized stored transcripts stop resume with a fork/new-session hint; they
never reuse an old local transcript or silently reset the upstream conversation.
Deselected skill files are removed from the generated plugin on the next run.

Persistent "always" approvals apply to matching actions in their existing
organization/workspace scope; explicit denies and Explore restrictions still
win. Redacted or truncated actions use a keyed fingerprint rather than raw secret
text or a shared `[redacted]` value as their persisted approval identity.

External runs use durable ownership and heartbeats. A stale heartbeat becomes
recoverable after a 240-second grace period; recovery fences the prior owner.
When a runner lease was recorded, another run is not admitted until cleanup is
confirmed. If the attempt never recorded a lease, recovery treats it as having
no authorized child process. An unresolved `closing` attempt stays blocked when
cleanup cannot be verified. The grace period is not a cleanup deadline; runner
or host failure can leave cleanup unconfirmed.

## Checks and coverage

Run relevant component suites when changing or deploying them:

```bash
(cd backend && source .venv/bin/activate && pytest -q)
(cd runner && source .venv/bin/activate && pytest -q)
(cd webapp && npm run test && npm run build)
```

Backend accessor and runner-handler tests cover the artifact RPC contract.
Runner artifact tests cover provisioning. Set `CLAUDE_TEST_BINARY` to a local
Linux x64 release binary to run the actual installer/cache/corruption contract;
its size/hash must match the checked-in pin. This uses a local shell and
filesystem fake—not a QEMU guest. Backend local-SSE contracts fail if
the installed SDK/bundled CLI differs from the production pins; a separate check
compares the bundled binary's size/hash with the runner manifest. These tests run
the real pinned CLI against scripted responses and cover native schemas, prompt
preservation on resume, child context/tool scopes, direct image/PDF input,
permission allow/ask/deny paths, MCP correlation, steering, selected skills,
native edits, todos and turn-limit completion without real provider calls.
They do not verify Anthropic token validity, billing or live model inference.
