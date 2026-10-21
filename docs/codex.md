# Codex

OpenCuria runs the official **Codex 0.163.0 standalone app-server package**
inside a Linux workspace. OpenCuria owns accounts, prompts, permissions, tools,
subagents and run lifecycle; Codex owns its model/tool loop. There is no protocol
translation gateway. The integration uses a pinned, experimental **internal
app-server API**, not a stable public SDK contract.

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
- **Root Plan** disables native shell and uses native `read-only` mode. Managed
  direct source edits ask for approval, while permitted shell/process actions
  follow the existing OpenCuria Plan policy. Plan is **not** an absolute read-only
  guarantee for the entire workflow.
- **General subagents** use Build/YOLO behavior even under a Plan parent.
  **Explore** uses safe read-only tools: no delegation, questions, process tools,
  plugins or mutation; only allowlisted read-only managed shell commands.
- Explicit global permission rules also force native read-only mode with native
  shell off, so managed actions go through OpenCuria's permission evaluator.
- OpenCuria `task` manages General/Explore children and nesting limits. Codex's
  native multi-agent system is disabled. **Computeruse children are explicitly
  unsupported for external harnesses**, as with Claude; there is no native fallback.
- OpenCuria supplies plugins/skills and managed tools. Codex-native plugins,
  hooks, discovery and other autonomous integrations are disabled. Images and
  tool screenshots use the shared authorized, no-follow media hydration path.
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
Mode changes keep stable signatures and re-evaluate the current role and policy.

Forking or editing starts a **fresh upstream thread** from the copied OpenCuria
message prefix, not a native Codex thread/fork. Neither a fork nor checkpoint
restore rolls back workspace files or other tool effects.

Each session has one encrypted, owner-fenced replacement checkpoint: at most
256 allowlisted durable files, 24 MiB raw and 34 MiB encoded. Auth, generated
config, logs and caches are excluded; opaque history/tool output can still
contain user data. Restore requires the same absolute workspace and `CODEX_HOME`
(`/workspace/.opencuria/harness/codex/<session-uuid>/config`). Transfers use 4 MiB
pieces below the shared 10 MiB file-write cap. Keep backend encryption keys and
persistent database storage backed up.

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
ripgrep and bubblewrap; it is not a lone binary bundled in this repository.
Linux x86_64 and aarch64 static musl packages are pinned for glibc/musl guests.
Guests need Python 3.10+ (`python3`), `sh`, `bash`, `setsid` and the kernel/libc
sandbox/publication support described in the [runner guide](../runner/README.md#pinned-external-runtimes).

Live browser/QEMU verification has covered **x86_64 only**. aarch64 release bytes
are pinned, but aarch64 runtime execution has not been verified. Docker/QEMU
transport support does not imply every guest kernel permits native sandboxing.
Usage comes from Codex token totals with resumed baselines subtracted. Displayed
cost is **unknown (recorded as zero)**, not an invoice or a promise of free usage.

## Optional real-runtime tests

Install normal backend/runner test dependencies first (see the component guides).
From the repository root on Linux x86_64:

```bash
python3 scripts/fetch-codex-test-runtime.py
(cd backend && .venv/bin/pytest -q apps/harness/tests/test_codex_core*.py)
(cd runner && .venv/bin/pytest -q tests/test_codex_artifacts.py)
```

The standalone stdlib setup script needs no Django configuration or provider
credentials. It downloads about **117 MB** on x86_64 (109 MB on aarch64), verifies
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

[release]: https://github.com/openai/codex/releases/tag/rust-v0.163.0
[openrouter]: https://openrouter.ai/docs/guides/guides/codex-cli
