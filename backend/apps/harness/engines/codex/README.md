# Codex engine internals

Runs the pinned **0.166.0** standalone Codex app-server inside the workspace over
private stdio and adapts its experimental internal API to OpenCuria's harness
contracts. Setup and tests: [backend README](../../../../README.md#codex-harness).

| Module | Responsibility |
| --- | --- |
| `catalog.py`, `models.json` | Pinned ChatGPT capabilities and model routing; OpenRouter uses the shared provider catalog. |
| `auth.py` | Resolves the provider account through organization/user ACLs; secrets never become public state. |
| `configuration.py` | Startup/thread overrides and provider wire mapping. |
| `protocol.py` | Bounded JSON-RPC stdio, request deadlines, server requests, transport failure. |
| `engine.py` | Thread/turn loop, owner lifecycle, streaming, steering, cancellation, checkpoints. |
| `tools.py` | OpenCuria extension tools, dispatched through shared permissions. |
| `events.py` | Native tool projection, attachments and child timelines. |
| `media.py` | Image hydration and isolated, bounded PDF text extraction (`pypdf`). |
| `session_store.py`, `state_io.py` | Encrypted, owner-fenced checkpoints and guest snapshot/restore. |

Shared plumbing lives in `engines/external_runtime.py` (auth, refresh, state) and
`engines/execution.py` (durable run ownership, events). The runner only provisions
and transports; it makes no model, prompt, tool or permission decisions.

## Invariants

- Engine and provider are fixed per chat. Model IDs are `chatgpt/<id>` or
  `openrouter/<vendor>/<model>`.
- No refresh token reaches the workspace. ChatGPT tokens are refreshed centrally
  (`providers/chatgpt_credentials.py`) and passed over stdio; the OpenRouter key goes
  only into the runner's private stdin environment preamble.
- Checkpoint writes require the exact session, run, owner token and thread. Dirty,
  missing or mismatched state never falls back to an older checkpoint.
- Clear `codex_dirty` only after terminal state, confirmed process-tree closure and
  a persisted checkpoint.
- Dynamic tool signatures are immutable on resume; incompatible changes need a fork
  or new chat. Role, depth and policy are checked on every dispatch.
- Native question/plan tools are replaced by OpenCuria's question/todo tools; no
  duplicate file/shell tools are advertised. Codex runs subagents natively.
- Steering sends `turn/steer` before acknowledging delivery; uncertain transport
  fails the run instead of replaying an upstream action.
- Timeline edits come from `fileChange` items only; `turn/diff/updated` is a
  cumulative snapshot and never creates rows.

Licensing: [third-party notices](../../../../../THIRD_PARTY_NOTICES.md#openai-codex).
