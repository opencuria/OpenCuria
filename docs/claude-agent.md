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
runtime-resolved aliases, not fixed model versions. Both modes use the same
configured permission rules as the native OpenCuria harness. Build allows tools
by default; the primary Plan agent asks before direct file edits/writes but
allows Bash, background processes, and registered MCP tools without additional
Claude-specific approval gates. Explicit `ask`/`deny` rules still apply.
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

## Messages while the agent runs

You can keep typing while a run is active (native and Claude Agent chats alike).
The message is queued above the composer and joins the running turn at its next
step: native runs add it after the current tool results, and Claude Agent
streams it to the CLI as a `next`-priority user message, which the CLI folds
into the turn after the current tool call. Delivered messages appear inline in
the transcript where the agent received them. If the turn ends first, the queued
message starts a follow-up run automatically; **Stop** discards the queue.
`POST /harness/sessions/{id}/steer` and the MCP tool `steer_harness_session`
expose the same behavior.

## Runtime and updates

Deploy matching OpenCuria backend and runner versions; apply migrations
`0028_claude_harness`, `0029_harnessrun`, and `0006_scheduledtask_harness_id`.
The backend pins `claude-agent-sdk==0.2.164`; the runner provisions Claude Code
CLI `2.1.292` at:

```text
/opt/opencuria/runtimes/claude-agent/2.1.292/claude
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
`artifacts/claude-agent/2.1.292/` in the runner state volume.

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
their credentials use OpenCuria's existing bridge. Selected skills are included
eagerly in the composed prompt and made available through a generated Claude
plugin in managed workspace state. Repository hooks are not automatically
enabled; OpenCuria does not create or edit a repository `CLAUDE.md`. Native tool
schemas and descriptions come solely from the pinned CLI; OpenCuria describes
only its managed MCP tools. Native WebFetch/WebSearch use Claude's behavior,
including for Explore, rather than OpenCuria Webfetch's URL protections.

Each run, including resume, rebuilds the OpenCuria prompt from the current mode,
project context, and selected skills. Native subagents receive their own scoped
project/run context. Explicit user image/PDF references (`@file:/workspace/...`
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
Runner artifact tests cover provisioning and, when pinned CLI bytes are
available locally, run the installer against them using a local shell and
filesystem fake—not a Docker or QEMU guest. Backend local-SSE contract tests run
the real pinned CLI against scripted responses and cover native schemas, prompt
refresh on resume, child context/tool scopes, direct image/PDF input, permission
hooks, MCP correlation, and turn-limit completion without real provider calls.
