# Backend

Django control plane: REST/Socket.IO API, agent harness (`apps/harness/`), plugins,
credentials and runner orchestration. Setup and processes: [root README](../README.md).

## Media

Prompt images (`@file:` mentions, workspace Markdown images) are shared by all
engines: up to 16 per turn, previews bounded by 2048 px and 1.5 MB, unreadable
images passed as paths. Tool images and PDFs are stored once under
`/workspace/.opencuria/tool-media/<sha256>.<ext>`; later turns reference the path
instead of base64.

## Workspace files

Clients never receive file contents over Socket.IO. They request signed,
path-bound links (`POST /api/v1/workspaces/{id}/files/links/`, upload links via
`.../files/uploads/`) and stream bytes through the raw ASGI transport under
`/api/v1/workspace-files/` (`apps/workspace_files/http.py`): `Range` downloads,
folders as `.tar.gz`, blurred 48 px previews, all-or-nothing uploads. Every request
re-checks workspace ownership and runs one guest process stream; at most 500 MB
per file and four transfers per workspace at a time.

## Codex harness

Engine internals: [Codex engine README](apps/harness/engines/codex/README.md).
Optional tests against the pinned runtime, from the repository root on Linux:

```bash
python3 scripts/fetch-codex-test-runtime.py
(cd backend && .venv/bin/pytest -q apps/harness/tests/test_codex_core*.py)
(cd runner && .venv/bin/pytest -q tests/test_codex_artifacts.py)
```

Artifacts go to the ignored `.opencuria/codex-test-runtime/`; tests use a local fake
Responses endpoint, never real credentials. Without artifacts these cases skip.
