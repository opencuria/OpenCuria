# AGENTS.md — OpenCuria Project Guide

> Single source of truth for AI coding agents. Read it in full before changing code.
> Living document: keep it short, drop what is outdated, add only what matters.

## 1. What is OpenCuria?

OpenCuria centrally provisions, manages, and monitors AI coding agents.

- `backend/` (Django + Ninja + Channels): control plane and agent harness
  (providers, tools, permissions, prompts, agentic loop). Owns all agent logic.
- `runner/` (Python, asyncio): dumb exec daemon. Runs Docker/QEMU workspaces,
  exposes lifecycle, exec, files, terminal, desktop, and git RPC. No agent logic.
- `webapp/` (Vue 3): dashboard for workspaces, chat, credentials, images.

Flow: `webapp` → REST/WS → `backend` → Socket.IO `harness:*` RPC → `runner` → workspace.

## 2. Goal: modular and extensible

Extend OpenCuria by reusing and extending existing structures, never by
duplicating or bypassing them:

- Credentials: `backend/apps/credentials/` (catalog + org instances,
  Fernet-encrypted, resolved server-side at workspace/plugin runtime).
- Plugins: `backend/apps/plugins/` (skills, MCP servers, credential
  requirements). New integrations belong here.
- Harness: `backend/apps/harness/` (`providers/`, `tools/` + registry,
  `permissions/`, `agents/` definitions, `prompts/` composer, `runner.py` loop).
- Runners: `runner/src/runtime/` and `interfaces/` abstractions (ABC + implementations).
- REST/MCP parity: every new REST capability needs the matching MCP tool.

Strictly follow SOLID, separation of concerns, and DRY. One concern per
layer, one implementation per behavior, composition over duplication.

## 3. Architecture rules

- Backend is Clean Architecture: `api.py` (thin validation/routing) →
  `services.py` (all business logic) → `repositories.py` (only ORM access).
  No business logic in API/Socket.IO layers, no ORM calls in services.
- All agentic knowledge lives in `backend/apps/harness/`. The runner never
  prompts, never decides, never owns provider/tool/permission logic.
- `webapp`: use only shadcn-vue components from `webapp/src/components/ui/`.
- Docs/setup details live in `README.md` and component READMEs; do not
  duplicate them here.

## 4. Coding rules

- English for code, comments, docstrings, commits, docs. Python 3.10+
  (`X | Y` unions), type hints on all signatures, docstrings on public APIs.
- PEP 8, 88 cols; `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`;
  dataclasses/Pydantic over raw dicts; stdlib → third-party → local imports.
- Async-first; wrap blocking calls in `asyncio.to_thread()`; never `time.sleep()`.
- `structlog` only, never `print()`; bind context (`workspace_id`, `task_id`) early.

## 5. Do's

- Do reuse the credential system for any new secret (env var, SSH key, token).
- Do build new capabilities as harness tools or plugins, not as one-off endpoints.
- Do extend existing ABCs, registries, and permission checks before adding new ones.
- Do keep harness system prompts precise and short: state role, scope, and hard
  constraints only; no background prose, no duplication with tool descriptions.
- Do add or update tests with every behavior change (happy path + auth/permission
  + one failure path) and keep the suite green.
- Do render secrets server-side only; never return, log, or embed plaintext secrets.

## 6. Don'ts

- Don't duplicate business logic across interface, service, or runtime layers.
- Don't put agent/prompt/permission logic in the runner or the frontend.
- Don't invent a parallel secret, config, plugin, or permission mechanism.
- Don't write long, verbose system prompts or repeat tool schemas in prompts.
- Don't add dependencies without justification; don't bypass `Todo`/permission gates.
- Don't commit directly to `main`, don't skip the pre-commit gate.

## 7. Verify before finishing

Run what your change touches:

```bash
cd backend && source .venv/bin/activate && pytest -q
cd runner && source .venv/bin/activate && pytest -q
cd webapp && npm run test
```

Commit gate: `./.githooks/pre-commit` must print `Ready to commit.` Fresh clone
first: `./scripts/setup-dev.sh`. After implementing, verify for real and embed
a workspace screenshot as `![label](path)` when it shows the working result.
