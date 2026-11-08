# AGENTS.md

Guide for AI coding agents. Read it in full before changing code. Keep it short.

## Project

OpenCuria provisions, manages and monitors AI coding agents.

- `backend/` (Django, Ninja, Socket.IO on Uvicorn): control plane and agent harness.
  Owns all agent logic: providers, tools, permissions, prompts, agentic loop.
- `runner/` (Python, asyncio): exec daemon for QEMU/KVM workspaces (lifecycle, exec,
  files, terminal, desktop, Git RPC). No agent logic.
- `webapp/` (Vue 3) and `mobile/` (Expo, React Native): clients.
- `packages/chat-core/`, `packages/client/`: shared chat rules and REST/Socket.IO client.

Flow: client → REST + Socket.IO `/frontend` → backend → `harness:*` RPC → runner → workspace.

## Extend, don't duplicate

Follow SOLID, separation of concerns and DRY. Extend existing structures:

- Secrets: `backend/apps/credentials/` (Fernet-encrypted, resolved server-side).
  Never return, log or embed plaintext secrets.
- Integrations: `backend/apps/plugins/` (skills, MCP servers, credential requirements).
- Agent capabilities: `backend/apps/harness/` (`providers/`, `tools/` registry,
  `permissions/`, `agents/`, `prompts/`, `runner.py`), not one-off endpoints.
- Runtimes: ABCs in `runner/src/runtime/` and `runner/src/interfaces/`.
- MCP server: `backend/apps/mcp_app/`, an adapter over the same services as REST.
  Keep about 30 task-oriented `<resource>_<verb>` tools with precise descriptions;
  extend a tool before adding one.

## Architecture

- Backend: `api.py` (validation, routing) → `services.py` (business logic) →
  `repositories.py` (ORM only). No business logic in API/Socket.IO, no ORM in services.
- Runner and frontends never prompt, decide or own provider/tool/permission logic.
- Webapp: only shadcn-vue components from `webapp/src/components/ui/`; use the shared
  `ModelPicker` and start list detail views with `DetailHeader`.
- Mobile: reuse `@opencuria/chat-core` and `@opencuria/client`; screens in
  `mobile/src/screens/` (never `mobile/src/app/`). Chat snapshots stay immutable.
- Import chat-core modules directly; never re-export them (`clientBoundaries.test.ts`).
- UI: less is more. Few controls, concise copy, one design language; reuse existing
  components and patterns before creating new ones.
- Harness prompts: role, scope and hard constraints only; never repeat tool schemas.

## Code

- English everywhere. Python 3.10+, type hints on all signatures, docstrings on
  public APIs, PEP 8 at 88 columns, dataclasses/Pydantic over raw dicts.
- Async-first: `asyncio.to_thread()` for blocking calls, never `time.sleep()`.
- `structlog` only, never `print()`; bind `workspace_id`, `task_id` early.
- No dependency without justification. Never bypass todo or permission gates.
- Never commit directly to `main`.

## Docs

READMEs cover only setup, operation, orientation and invariants that code cannot
show. No feature specs, changelogs, update notes or implementation narratives;
code and tests document behavior. Keep every Markdown file short and update or
delete outdated text instead of appending. Do not add plan, notes or report files
to the repository.

## Verify

Add or update tests with every behavior change (happy path, auth/permission, one
failure path). Run the checks relevant to your change:

```bash
cd backend && .venv/bin/pytest -q
cd runner && .venv/bin/pytest -q
npm run type-check
npm run test
```

Install npm dependencies with `npm ci` at the repository root only. Fresh clone:
`./scripts/setup-dev.sh`. Mobile device checks: `mobile/README.md`. After
implementing, verify for real and embed a screenshot as `![label](path)` when it
shows the result.
