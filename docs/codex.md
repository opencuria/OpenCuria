# Codex

OpenCuria runs the official **Codex 0.166.0 standalone app-server package**
inside a Linux workspace. OpenCuria owns accounts, permission boundaries and
run lifecycle; Codex retains its native prompt, tools and multi-agent loop.
There is no protocol translation gateway. The integration uses a pinned, experimental **internal
app-server API**, not a stable public SDK contract.

## Upgrade review: 0.163.0 → 0.166.0

Reviewed the [official changelog][changelog], releases
[0.164.0][release-164], [0.165.0][release-165] and [0.166.0][release],
their protocol/configuration changes, and the actual bundled model catalog.

| Change | OpenCuria handling |
| --- | --- |
| Standalone executor added to the release package | Pin and verify `bin/codex-exec-server` alongside the other helpers, including its metadata and immutable installed bytes. Update both architecture pins and the managed ripgrep PATH. |
| GPT-6.1 Terra | Include the runtime's model name and supported efforts, including `ultra`, in account-scoped discovery and validation. The default stays GPT-6.1 Sol with OpenCuria's high effort. |
| Fresh memory profiles default to v2 | Checkpoint and restore both `memories/` and `memories_v2/` markdown and their SQLite databases, including `memories_v2_1.sqlite`, retaining size, path and symlink restrictions. Both versions use the existing memory timeline display. Existing v1 profiles retain upstream's version selection. |
| Reasoning changes during conversations; resume, interrupt, compaction and shutdown fixes | Retain per-turn effort and explicit permission overrides. Drain late events after Stop even when the timeline rejects them with cancellation, so the terminal event still permits a fenced checkpoint. Exercise continuation, steering, child cleanup and usage against the new runtime. |
| Item lifecycle timestamps and generated-image identifiers | Items remain flat on the wire. Local app-server uses inline image storage; existing inline/saved-path previews remain compatible, including authorization checks. Remote `fileId` resolution would need a separate attachment-store integration if that storage mode is introduced. |
| Attached native forks and Vertex AI provider | Optional future integration work. OpenCuria forks keep their current copied-history semantics; adding Vertex requires the shared provider/account system. Neither is enabled implicitly. |
| CLI presets, inline model/permission commands, transcript selection, image previews and `doctor --network-only` | Terminal UI/diagnostic features do not require web/mobile adapters. OpenCuria retains its model picker, permissions and timelines. |
| Enterprise MCP sign-in, coordinated token refresh and remote credential masking | Native MCP behavior stays upstream-owned; managed plugin tools retain OpenCuria's credential and permission boundaries. Native interactive sign-in is separate from OpenCuria provider-account login. |

External-agent memory import remains an upstream opt-in. No automatic import or
new provider login flow is added. Future UI work could expose native memory
summaries, attached forks, speed tiers or safety-block follow-up choices through
the existing services if those capabilities are needed.

## Connect and start

1. Connect a **ChatGPT subscription** account or an **OpenRouter** API-key account
   in provider settings, and select the account you want to use.
2. Create a chat with the **Codex** harness, then choose that provider's model and
   supported reasoning effort. No workspace-side `codex login` is needed.
3. Continue in that chat. Its engine and provider are fixed; model changes must
   stay within the bound provider. Each answer captures the then-selected account;
   changing the selection during an answer does not reroute that answer. Scheduled
   runs resolve current selections when they execute, not when saved.

Direct OpenAI API keys and arbitrary OpenAI-compatible providers are **not in
scope**. ChatGPT's catalog is pinned bundled capability metadata, not a live list
of your subscription entitlements. Efforts are model-specific (`ultra` only where
advertised); unknown context/output limits are not invented. OpenRouter uses the
existing account/revision-keyed catalog, filtered for tool support, and maps
`openrouter/vendor/model` to Codex's `~vendor/model`. A listed model may still
reject the actual Responses API or tool protocol at runtime; catalog presence is
not a compatibility guarantee. See [OpenRouter's Codex guide][openrouter].

## Permissions and capabilities

- **Build** uses Codex's native shell/patch tools with `danger-full-access` and
  approval policy `never`. Treat the workspace as fully writable by the agent.
- The bundled code-mode host stays enabled: models such as GPT-6.1-Sol require
  `exec`/`wait` wrappers even when optional code mode is disabled. Wrapped
  OpenCuria tools still pass through the shared permission dispatcher.
- **Root Plan** disables native shell and uses native `read-only` mode. Managed
  MCP source edits ask for approval, while permitted process actions
  follow the existing OpenCuria Plan policy. Plan is **not** an absolute read-only
  guarantee for the entire workflow.
- Native Codex subagents inherit Codex's execution context and sandbox. Their
  activity and answers appear in the existing OpenCuria subagent timelines.
  They share the parent app-server, account and checkpoint, not a second
  OpenCuria-managed inference run. Upstream decides child tool inheritance;
  in the pinned native v1 delegation, client dynamic tools are not inherited.
  Child shell/file tools remain native and are projected into the child timeline.
  Codex's `subAgentActivity` format exposes the agent path, not readable task
  instructions. These children retain their name and show an explicit notice
  that instructions are unavailable. The initial message records prompt
  availability in its delegation metadata; classic `collabAgentToolCall`
  prompts are preserved when provided.
- Explicit global permission rules also force native read-only mode with native
  shell off, so managed actions go through OpenCuria's permission evaluator.
- The only additional OpenCuria tools are `question`, `todowrite`, `process_*`
  and registered MCP tools—the same integration surface as Claude. Native
  question/plan-update tools are disabled in favor of persistent OpenCuria gates.
  Shell, files and delegation are native Codex, not duplicate managed tools.
- The native base prompt is not replaced. OpenCuria adds only the
  [shared product context](harness-prompts.md) as developer instructions:
  workspace boundaries, permitted local setup, and Markdown file/media rendering.
  Coding roles, tool guidance and planning instructions remain Codex-owned.
  Codex discovers project instructions, skills, MCP configuration and plugins
  natively. Authorized workspace `.codex/config.toml` is loaded as trusted;
  the session-specific `CODEX_HOME` does not import another user's host config.
  Selected OpenCuria skills are materialized as native `SKILL.md` files.
- Native search, multi-agent and image capabilities keep upstream defaults;
  native memory is enabled explicitly (it is opt-in upstream). Actual availability
  remains model/account-dependent. Memory may issue its own background model
  requests; root-turn token totals do not necessarily include these requests.
  Memory markdown and databases are included in bounded encrypted checkpoints.
  Both legacy `memories/` and current `memories_v2/` are retained; fresh profiles
  use upstream's v2 default, while existing v1 memory roots remain compatible.
- Tool display names are harness-independent: `task`, `websearch`, `memory`,
  `image_generation`, and `tool_call` for unmapped native/MCP calls. Image results
  use bounded attachments; workspace previews require authorized no-follow reads.
  Memory citations and observed native memory operations receive their own rows;
  internal background consolidation is not fabricated as a tool call.
- PDFs are **text only**, parsed by the mandatory `pypdf==6.21.1` backend dependency
  in an isolated process: 20 MiB input, 256 MiB address space, 5 seconds soft CPU
  (6 seconds hard), 10 seconds wall time, 100 pages and 512 KiB extracted UTF-8.
  No images, layout preservation or OCR. Encrypted, malformed, oversized and
  image-only documents fail explicitly rather than being silently omitted.

## Continuation and recovery

A completed answer checkpoints the upstream thread. A confirmed user **Stop**
also supports same-thread continuation after terminal interruption, process-tree
cleanup and successful checkpoint persistence. An uncertain failure remains
**dirty**: fork or start a new chat instead of silently restoring stale history.
Changes to registered dynamic tool schemas also require a fork/new chat.
Older Codex chats still bound to the former full OpenCuria tool registry need a
fork/new chat; native tool signatures are never rewritten inside an existing
upstream thread. The 0.166.0 runtime upgrade keeps the managed tool signatures.
Mode changes keep stable signatures and re-evaluate the current role and policy.

Forking or editing starts a **fresh upstream thread** from the copied OpenCuria
message prefix, not a native Codex thread/fork. Neither a fork nor checkpoint
restore rolls back workspace files or other tool effects.

Each session has one encrypted, owner-fenced replacement checkpoint: at most
256 allowlisted durable files, 50 MiB raw and 68 MiB encoded. Auth, generated
config, logs and caches are excluded; opaque history/tool output can still
contain user data. Restore requires the same absolute workspace and `CODEX_HOME`
(`/workspace/.opencuria/harness/codex/<session-uuid>/config`). Transfers use 4 MiB
pieces below the shared 10 MiB file-write cap. Keep backend encryption keys and
persistent database storage backed up.

The protocol buffers up to 4,096 notifications within a 32 MiB byte ceiling, so
short output bursts can settle while tool and authorization RPCs remain live.
Exceeding either ceiling fails explicitly with an event-buffer recovery error;
notifications are never silently dropped.

## Authentication and trust boundary

Credentials remain in the existing encrypted provider-account store. Native
ChatGPT and Codex share centralized, database-leased OAuth refresh; migration
`0037_provider_refresh_lease` is required. Refresh rereads the exact captured
account and fences stale owners; unknown accounts, revocation and rejected/401
auth fail closed through the existing access controls. Reconnect the account
when authorization cannot be refreshed.

ChatGPT access/account tokens are delivered through private app-server stdio.
The refresh token is **not** a workspace access token and is not sent to Codex.
OpenRouter's key uses the runner's private stdin environment preamble, not argv
or persisted config. The runner supplies a disposable HOME and fixed PATH,
excluding ambient credentials; authentication values must not enter public
messages, events or engine metadata. These are trusted same-user workspace
processes: a sufficiently privileged process can inspect memory/environment.
This is **not OS credential isolation from workspace code**.

## Deployment and accounting

Deploy matching backend and runner code, install backend requirements and apply
migrations before serving traffic. Do not substitute a system CLI, run CLI
self-update, or request `latest`. The runner downloads the exact official
[release][release] on demand and verifies size, SHA-256, layout and every installed
file before execution. The complete package includes app-server, code-mode host,
exec-server, ripgrep and bubblewrap; it is not a lone binary bundled in this
repository.
Linux x86_64 and aarch64 static musl packages are pinned for glibc/musl guests.
Guests need Python 3.10+ (`python3`), `sh`, `bash`, `setsid` and the kernel/libc
sandbox/publication support described in the [runner guide](../runner/README.md#pinned-external-runtimes).

Live browser/QEMU verification has covered **x86_64 only**. aarch64 release bytes
are pinned, but aarch64 runtime execution has not been verified. QEMU
transport support does not imply every guest kernel permits native sandboxing.
Usage comes from Codex token totals with resumed baselines subtracted. Displayed
cost is **unknown (recorded as zero)**, not an invoice or a promise of free usage.
The context indicator uses the latest request's tokens and effective context
limit reported by Codex. It updates during a run and survives chat reloads;
the answer footer retains cumulative input/output tokens for the whole turn.

## Optional real-runtime tests

Install normal backend/runner test dependencies first (see the component guides).
From the repository root on Linux x86_64:

```bash
python3 scripts/fetch-codex-test-runtime.py
(cd backend && .venv/bin/pytest -q apps/harness/tests/test_codex*.py)
(cd runner && .venv/bin/pytest -q tests/test_codex_artifacts.py)
```

The standalone stdlib setup script needs no Django configuration or provider
credentials. It downloads about **155 MB** on x86_64 (145 MB on aarch64), verifies
approved release size/SHA-256 and safe layout, and installs to the ignored
`.opencuria/codex-test-runtime/`. Repeating it verifies the archive and every
installed file, including helpers, before checking the exact version. Changed
installs fail rather than being silently replaced. `--archive /path/package.tar.gz`
can seed an offline install, subject to identical checks. It never searches PATH
for an arbitrary Codex executable or downloads a floating release.

Tests discover `package/bin/codex-app-server` and `package.tar.gz` there by default.
For a separately verified package, explicitly set `OPENCURIA_TEST_CODEX_BINARY`
(backend) or `OPENCURIA_TEST_CODEX_PACKAGE` (runner). Without the optional artifacts,
only real-runtime cases skip. The real installer test currently targets x86_64;
backend runtime tests can use the native architecture installed by the script.

Core tests drive the actual pinned app-server against a local fake Responses SSE
endpoint: no paid inference or real OAuth credentials. Coverage includes managed
and native tools, Plan/Explore restrictions, cancellation, cold checkpoint restore
and continuation. Runner tests verify all release bytes and the real managed
installer/protocol. These do not establish live provider compatibility or account
entitlement. CI provisions the pinned runtime before backend tests; runner
real-package testing remains opt-in. Internal contracts are mapped in the
[engine README](../backend/apps/harness/engines/codex/README.md).

Licenses and redistribution obligations are in
[Third-Party Notices](../THIRD_PARTY_NOTICES.md#openai-codex).

[release]: https://github.com/openai/codex/releases/tag/rust-v0.166.0
[release-164]: https://github.com/openai/codex/releases/tag/rust-v0.164.0
[release-165]: https://github.com/openai/codex/releases/tag/rust-v0.165.0
[changelog]: https://learn.chatgpt.com/docs/changelog
[openrouter]: https://openrouter.ai/docs/guides/guides/codex-cli
