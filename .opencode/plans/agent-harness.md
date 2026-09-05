# Plan: OpenCuria wird eigenes Agent-Harness (Backend, nach OpenCode)

Stand: 2026-09-05. Geklaerte Entscheidungen: Provider-Config **org-weit in DB**,
Harness greift via **Runner als Exec-Daemon** auf Workspaces zu, Datenmodell
**neu nach OpenCode**, Modi/Permissions mit **OpenCode-Defaults**.
Explizit **keine Backwards-Kompatibilitaet** zur aktuellen Agent-Version.

## 1. Zielarchitektur

```
Backend-Prozess (Daphne/Django)
┌─────────────────────────────────────────────────┐
│ REST (Ninja) + Socket.IO (/frontend, /ws/runner)│
│ apps/harness/  <- NEU, einzige Business-Logik   │
│  ├─ runner.py      (Agentic Loop, asyncio.Tasks)│
│  ├─ providers/     (ABC + OpenRouter)           │
│  ├─ tools/         (ABC + Registry + 9 Tools)   │
│  ├─ permissions/   (Evaluator + Requests)       │
│  ├─ agents/        (build/plan/general/explore) │
│  ├─ prompts/       (System-Prompt-Composer)     │
│  └─ access/        (WorkspaceAccessor ABC)      │
│        ▲ RunnerWorkspaceAccessor (Socket.IO-RPC)│
└────────┼────────────────────────────────────────┘
         │ harness:exec/read/write/list/... (neu)
         ▼
Runner (dummer Exec-/Datei-Server, kein Agent-Wissen)
  WorkspaceService + Docker/QEMU Runtime (bleibt)
```

**Harness-Laufzeit:** v1 als sauber gekapseltes Modul mit eigenem
`HarnessService` + `asyncio.Task`-Registry im Backend-Prozess (Lebenszyklus an
`sio_server`/`asgi` gehaengt). Schnittstellen (`Runner`, `ProviderAdapter`,
`WorkspaceAccessor`) so geschnitten, dass es spaeter ohne Umbau in einen
eigenen Prozess/Worker (Celery/Django-Q/Standalone) ausgelagert werden kann.
Kein Agent-CLI mehr im Workspace.

## 2. Backend: neue App `apps/harness/`

Nach bestehender Clean Architecture: `api.py` -> `services.py` ->
`repositories.py` -> ORM. Neu:

| Modul | Inhalt |
|---|---|
| `providers/base.py` | `ProviderAdapter`-ABC: `chat_stream(model, messages, tools, opts) -> AsyncIterator[Delta]`; Typen `LLMMessage`, `ToolSchema`, `Usage`; `ProviderError`-Hierarchie |
| `providers/openrouter.py` | Einziger Adapter v1: OpenAI-kompatibel (`base_url` Default `https://openrouter.ai/api/v1`), SSE-Streaming, Timeouts, Tool-Calls, Reasoning-Deltas. `ProviderRegistry` fuer spaetere Adapter (nur registrieren, kein Umbau) |
| `providers/models.py` | `ProviderConfig(org UNIQUE, api_key_encrypted Fernet, base_url, default_model, small_model)` - org-weit, via Credentials-Verschluesselung |
| `tools/base.py` | `Tool`-ABC: `name, description, args_schema (pydantic->JSON-Schema), permission_key, title(args)->str, async execute(args, ctx)`; `ToolContext{session, workspace_id, accessor, agent, directory}`; Before/After-Hooks als Extension-Point (Plugins/MCP spaeter) |
| `tools/` v1 | `read, write, edit, bash, glob, grep, list, todowrite, task` (+ optional `webfetch`). Alle ueber `WorkspaceAccessor`, nie direkt Shell/FS im Harness |
| `access/base.py` | `WorkspaceAccessor`-ABC: `exec_stream, exec_wait, read_file, write_file, list_dir, stat` - Pfade auf `/workspace` sandboxen |
| `access/runner_accessor.py` | Implementierung via Socket.IO-RPC zum Runner (s.u.), `request_id`-Korrelation, Timeouts, Cancel |
| `permissions/` | `PermissionEvaluator`: Regeln `{"*": ask, bash: allow, ...}`, Wildcards (`*`/`?`), **last-match-wins**, `external_directory`/`doom_loop`, Merge global->Agent->Modus. `PermissionRequest`-Modell + `updated/replied`-Flow |
| `agents/` | Statische Definitionen als Code (statt DB, wie OpenCode `.opencode/agents/*.md`): `build` (primary, alles allow), `plan` (primary, `edit/bash` ask), `general`+`explore` (subagents), `title/compaction` (hidden, small_model). Felder: `mode, description, model-override, permission, prompt, steps, color` |
| `prompts/` | Composer: Agent-Prompt + `AGENTS.md`-Walk-up (via Accessor) + Instructions/Skills/References + Tool-Liste + Subagent-Beschreibungen + Datum/Env + Kompaktierungs-Kontext |
| `runner.py` | Agentic Loop: Nachrichten aus DB aufbauen -> Provider-Call (Tools nach Permission/Modus gefiltert) -> `step-start` -> Deltas streamen -> Permission-Gate -> Tool ausfuehren -> `tool completed/error` -> `step-finish{cost,tokens}` -> repeat bis Text-only / `steps`-Budget / Abort / Doom-Loop (3x gleicher Tool+Input -> ask). Cancel = `asyncio.Task.cancel()` |

## 3. Datenmodell (alt loeschen, neu nach OpenCode)

Loeschen (Migration, kein Kompat-Code): `AgentDefinition`, `AgentCommand`,
`AgentCredentialRelation*`, alter `Chat`/`Session`, `TaskType.RUN_PROMPT`/
`CANCEL_SESSION`, `build_run_command`, `task:run_prompt`-Pfad.

Neu in `apps/harness/models.py`:
- `HarnessSession{id, workspace FK, parent FK self (Subagent), title, mode plan/build, agent_name, model, status busy/idle, cost, tokens JSON}`
- `HarnessMessage{id, session FK, role user/assistant, model, provider, cost, tokens, finish, error, created/completed}`
- `HarnessPart{id, message FK, type: text|reasoning|tool|step-start|step-finish|subtask|patch|agent, state: pending|running|completed|error, call_id, input/output/title/metadata JSON}`
- `PermissionRequest{id, session, message, call_id, tool, pattern, title, status pending|approved|rejected, remember}`
- `Todo{id, session, content, status pending|in_progress|completed|cancelled, priority, order}`

Behalten: `Workspace` (minus `agent_type`), `Runner` (nur noch Exec-Faehigkeit),
`Task` nur fuer Workspace-Lifecycle. Credentials weiter als Env/Files-Injektion
in den Accessor-Kontext.

## 4. Runner-Umbau (klein, gezielt)

- Runner bleibt Exec-Daemon: Lifecycle, Heartbeat, Terminal, Desktop,
  `list/read/upload/download` bleiben.
- **Neu:** generische Harness-RPC-Events statt `task:run_prompt`:
  `harness:exec_stream`, `harness:exec_wait` (mit Timeout/Cancel, getrennte
  stdout/stderr, Exit-Code-Streaming), `harness:read_file`,
  `harness:write_file` (atomar, mode), `harness:list/stat`. Implementiert als
  duenne Handler ueber existierendem `WorkspaceService` (+ fehlende
  `write_file`-Primitive ergaenzen, `/workspace`-Sandbox behalten).
- **Raus:** `task:run_prompt`/`task:cancel_prompt`, Configure-/Run-Command-Bau,
  `output:chunk/complete/error`-Pfad. Workspace-Image kann Copilot/Claude/
  Codex-CLIs verlieren (ripgrep, git, node bleiben).

## 5. API + Sockets (Backend<->Frontend)

REST (Ninja, mit feingranularen API-Key-Permissions wie bisher):
`ProviderConfig` CRUD, Sessions (`GET/POST /workspaces/{id}/sessions/`,
`POST .../message`, `POST .../abort`), Parts/Todos/Diff, Permissions
(`POST /sessions/{sid}/permissions/{pid}` `{response: once|always|reject}`),
Modus-Wechsel pro Session.

Socket.IO `/frontend` (bestehende `subscribe_workspace`-Mechanik
wiederverwenden): `message.part.updated{delta}`, `permission.updated/replied`,
`session.status/idle`, `todo.updated`, `session.diff`, `subtask.started/finished`
(Child-Session verlinkt). Alt (`session:output_chunk/completed/failed` als
reiner Text-Stream) entfaellt.

## 6. Frontend (OpenCode-Paritaet im OpenCuria-Design)

- `Session`-Typ -> Block-Modell (`parts[]`): Text (bestehende
  Markdown-Pipeline), Reasoning (einklappbar), Tool-Cards
  (`Collapsible`+`Badge`: pending dim -> running Spinner+Titel -> completed
  Output / error rot), `step-finish` (Kosten/Tokens), Todos (Checkliste), Diffs,
  Subtasks (verschachtelte Child-Session mit Navigation).
- `permission.updated` -> Modal (once/always/reject + Pattern-Vorschau),
  `question`-Tool -> Formular.
- `ChatInput`: Plan/Build-Toggle (Tab), Modell-Picker (aus ProviderConfig),
  `@file`/`@agent`-Autocomplete, bestehende Skills/Files-Picker wiederverwenden.
- `ChatContainer`: Stick-to-bottom nur wenn bereits unten (statt immer),
  inkrementelles Rendering fuer grosse Tool-Logs, sonst shadcn-vue-Bestandteile
  (`collapsible, card, badge, tooltip, scroll-area, skeleton`) + Lucide-Icons,
  keine neuen Design-Tokens.

## 7. Tests (Pflicht pro AGENTS.md 6.10)

- Backend `pytest`: Provider-Adapter gegen Mock-SSE, Permission-Evaluator
  (last-wins, Wildcards, Modus-Merge), Tools gegen Fake-Accessor (happy path +
  Sandbox-Verletzung + Runner-Fehler), Loop-Integration
  (Fake-Provider+Accessor: Multi-Step, Abort, Doom-Loop, Kosten), API-Vertraege
  (Auth/Org-Scope/Key-Permissions), Subagent-Vererbung/Tiefe.
- Runner `pytest`: neue Harness-Handler (exec/read/write, Sandbox,
  Cancel/Timeout).
- Webapp `vitest`: Block-Parsing/Reducer, Permission-Modal,
  Subagent-Rendering.
- Gate: `./.githooks/pre-commit` muss `Ready to commit.` melden; kein Push ohne
  gruene Matrix.

## 8. Meilensteine (jeweils lauffaehig auf aktuellem Branch committen)

1. **M1 Provider-Basis:** `ProviderAdapter`-ABC + OpenRouter + `ProviderConfig`
   (org-weit) + Tests.
2. **M2 Workspace-Zugriff:** `WorkspaceAccessor`-ABC + Runner-RPC (`harness:*`)
   beidseitig + Sandbox-Tests.
3. **M3 Tools + Permissions:** 9 Standard-Tools + Evaluator +
   `PermissionRequest`-Flow + Tests.
4. **M4 Loop + Modi + Prompts:** Agentic Loop, Plan/Build, System-Prompts,
   Steps/Budget/Abort/Doom-Loop + Tests.
5. **M5 Subagents + Todos:** `task`-Tool, Child-Sessions, Tiefe, Todos + Tests.
6. **M6 Persistenz/API/Sockets:** Modelle, REST, Frontend-Events, Cancel.
7. **M7 Frontend-Chat:** Block-UI, Permission-Modal, Modus/Modell-Picker,
   Subagent-Visualisierung.
8. **M8 Cutover:** AgentDefinition/AgentCommand + alte Pfade loeschen,
   Migration ohne Kompat, Doku, Full-Gate gruen.

Groesstes Risiko: Streaming-Stabilitaet (Provider-SSE -> Runner-Exec ->
Frontend-Socket) - deshalb M1/M2/M4 jeweils mit Fake-Gegenstellen isoliert
testen, bevor M6/M7 integrieren.
