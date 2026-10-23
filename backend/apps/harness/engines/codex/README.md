# Codex engine internals

The pinned **0.163.0** standalone app-server runs inside the workspace over private
stdio. This package adapts its experimental internal API to OpenCuria's existing
harness contracts; it is not a stable public SDK integration. User setup,
operations, limitations and reproducible tests: [Codex guide](../../../../../docs/codex.md).

## Architecture map

| Module | Responsibility |
| --- | --- |
| `catalog.py`, `models.json` | Pinned bundled ChatGPT capabilities and namespaced routing; OpenRouter uses the shared provider catalog. |
| `auth.py` | Resolve the captured provider account through existing organization/user ACLs; private auth values never become public state. |
| `configuration.py` | Authoritative startup/thread overrides, provider wire mapping, minimal integration overrides. |
| `protocol.py` | Bounded JSON-RPC stdio, request deadlines, server requests and terminal transport failure. |
| `engine.py` | Thread/turn loop, managed owner lifecycle, streaming, steering, cancellation and checkpoint finalization. |
| `tools.py` | Small OpenCuria extension surface and invocation through shared permissions. |
| `events.py` | Native tool projection, attachments and native child timelines. |
| `media.py` | Checked image hydration and isolated bounded PDF text extraction through shared media/parser code. |
| `session_store.py`, `state_io.py` | Encrypted owner-fenced checkpoint replacement and strict guest-side snapshot/restore. |

The common `engines/external_runtime.py` prepares auth, refresh and state ports;
`engines/execution.py` retains durable run ownership and emits existing session
and message events. Runner artifact/process code only provisions and transports;
it does not decide model, prompt, tool or permission policy.

## Contracts

- Admission fixes engine/provider; model IDs are `chatgpt/<id>` or
  `openrouter/<vendor>/<model>`. Legacy/generic default routes remain compatible;
  they are not a way to change an existing Codex chat's provider.
- Every answer resolves its immutable account snapshot, registers provider use
  before contacting upstream, and retains revocation/current-owner fencing.
  Native and Codex ChatGPT auth share the database-leased credential service in
  `providers/chatgpt_credentials.py`; the execution-supplied refresh callback has
  a nine-second bound and must return the same provider/connection identity.
- ChatGPT uses ephemeral `account/login/start` (`chatgptAuthTokens`) and refresh
  replies through stdio, without sending refresh tokens. OpenRouter uses
  `OPENCURIA_CODEX_API_KEY` in the private runner stdin environment preamble.
  Secrets are repr-hidden/redacted, never persistent engine metadata. Trusted
  same-user workspace processes are not an OS credential-isolation boundary.
- `on_binding(external_id, metadata)` merges nonsecret metadata under the current
  run fence, including CLOSING. An initial empty ID marks `codex_dirty=True`
  before launch; only a real upstream UUID binds a thread. Clear dirty only after
  terminal state, confirmed process-tree closure and persisted checkpoint.
- `on_owner` phases are `reserved`, `closing`, `released`. Runner release confirms
  tree closure, not answer finalization; durable ownership survives checkpoint
  and final binding/event writes.
- `CodexSessionStore` writes require exact `session_id`, `run_id`, `owner_token`
  and external thread. One encrypted generation replaces the previous row.
  Dirty/missing/mismatched state never falls back to an older generation.
- Codex owns native multi-agent execution. Native child threads project through
  the shared external-child timeline adapter and share the root owner/account
  and checkpoint. OpenCuria does not advertise its `task` dispatcher to Codex.
- Native base instructions remain untouched; `developerInstructions` contains
  only the shared identity/proactivity extension. Native workspace configuration,
  AGENTS and skill discovery remain enabled. Selected skills use native files.
- Native question/update-plan tools are replaced by OpenCuria's persistent
  question/todo tools. Process and registered MCP tools are the other extensions.
- Dynamic signatures are immutable on resume; incompatible registry/schema
  changes require fork/new-chat. Current role, depth and policy are checked at
  every dispatch, so mode changes do not change the registered ceiling. `thread/resume` gets neither unsupported dynamic
  tool overrides nor fabricated native history. Fork/edit instead starts a new
  upstream thread from copied OpenCuria history; workspace effects remain.
- Steering sends `turn/steer(expectedTurnId=...)` before acknowledging delivery.
  Explicit rejection leaves followup work pending; uncertain transport fails the
  run rather than replaying an unknown upstream action.

## Capabilities and limits

Build/General use native `dangerFullAccess`/`never`. Root Plan/Explore disable
native shell under `readOnly`/`never`; explicit permission rules also require
managed extension actions. Native subagents inherit the native sandbox.
Registered MCP edits and permitted process actions retain existing Plan policy.
No duplicate OpenCuria file/shell tools are advertised.

Images use shared authorized no-follow reads. Mandatory `pypdf==6.21.1` extracts
text only in an isolated parser (256 MiB, 5-second soft CPU, 10-second wall,
100 pages, 512 KiB text); there is no OCR or PDF image support.
Checkpoints allow 256 files, 24 MiB raw/34 MiB encoded, including native memory
markdown/databases and child rollouts, but excluding auth/config/logs/caches.
The absolute workspace and `CODEX_HOME` must match; 4 MiB restore pieces respect
10 MiB writes. Confirmed Stop plus cleanup/checkpoint allows continuation;
uncertain failure requires fork/new-chat. Neither path rolls back files.
Runtime token totals supply usage; unknown billed cost is zero, not an invoice.
ChatGPT metadata is not an entitlement list; OpenRouter catalog tool support does
not guarantee real Responses/tool compatibility. See the guide for deployment,
architecture coverage and licensing, rather than relying on system CLI defaults.
