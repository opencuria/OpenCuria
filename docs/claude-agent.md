# Claude Agent

Claude Agent is a separate harness: the backend uses the pinned Python SDK and
workspace transport; the pinned Claude Code CLI runs as a managed process in
the selected Linux workspace.

## Connect and chat

In **Settings → Providers → Claude Agent**, choose **API token** or
**Subscription token** and paste a token authorized for your own account. There
is no Anthropic browser sign-in or managed OAuth flow. OpenCuria stores the
personal token using its encrypted credential store; the UI and APIs return
connection metadata, not the token. OpenCuria does not refresh tokens or manage
their expiry; to reconnect, paste a valid token.

In a new chat, choose **Claude Agent Build** or **Claude Agent Plan**. Chats keep
their selected harness; start a new chat to choose another. Model and effort use
the shared model picker, which offers `sonnet`, `opus`, and `haiku`. These are
runtime-resolved aliases, not fixed model versions. Both modes use OpenCuria
permission gates; Plan does not switch to Build automatically.

API tokens are passed as `ANTHROPIC_API_KEY`; subscription tokens as
`CLAUDE_CODE_OAUTH_TOKEN`. Connections are personal to their owner within the
active organization, not shared organization credentials. Scheduled tasks
resolve the owner's current personal connection at each run and never copy its
token into a schedule or occurrence snapshot. Existing schedules default to the
native harness. Usage and billing follow the account for the token you provide;
CLI-reported cost is usage information, not an Anthropic invoice. See Anthropic's
[Claude Code legal and compliance information](https://docs.anthropic.com/en/docs/claude-code/legal-and-compliance).

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
harness environment, and one personal credential. Ambient runner credentials
are not forwarded. OpenCuria passes the credential through the CLI process
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
enabled; OpenCuria does not create or edit a repository `CLAUDE.md`.

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
transparently switch harnesses or replay Claude state in another harness.

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
filesystem fake—not a Docker or QEMU guest.

The opt-in browser E2E runs the actual pinned SDK and CLI through tool-use and
continuation turns against a local canned Messages fixture and synthetic
workspace accessor. It covers chat, root agent, SDK subagent, OpenCuria question
gate, settings UI, and persistence using dummy tokens. The CLI protocol is real;
model replies are scripted, with no real inference or Anthropic account call. It
does not use a native runner, Docker workspace, or QEMU guest. See the [local E2E
guide](../e2e/integration/README.claude-agent-live.md) for prerequisites and
commands; it requires Linux namespace setup and a separately started test
frontend.
