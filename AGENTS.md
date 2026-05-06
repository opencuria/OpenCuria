# AGENTS.md — opencuria Project Guide

> This file is the single source of truth for AI coding agents working on this
> codebase. Read it in full before making any changes.


>This is a living document. Be sure to make changes as needed. Remove items that are not so important or outdated, and add new important items. The goal is to keep this document relatively compact while still containing all the important information.

---

## 1. What is opencuria?

opencuria is a platform for **centrally provisioning, managing and monitoring AI
coding agents** (GitHub Copilot CLI, Claude Code, OpenAI Codex, etc.).

It consists of three components:

| Component | Tech | Status | Purpose |
|-----------|------|--------|---------|
| `runner/` | Python 3.10+, asyncio | **Implemented** | Dumb executor — runs Docker containers or QEMU/KVM VMs and executes commands sent by the backend |
| `backend/` | Django + Django Ninja + Channels | **Implemented** | Central control plane — owns agent definitions, assigns tasks to runners, stores results, exposes REST + WebSocket API |
| `webapp/` | Vue 3 | **Implemented** | Dashboard for managing agents, workspaces, credentials, conversations |

---

## 2. Architecture Overview

```
                                       ┌──────────────┐
                                       │   Frontend   │
                                       │   (Vue)      │
                                       └──────┬───────┘
                                              │ REST / WS
                                              ▼
┌─────────────┐  WebSocket (socketio)  ┌──────────────┐
│   Runner    │◄──────────────────────►│   Backend    │
│  (Python)   │   bearer auth          │  (Django)    │
└──────┬──────┘                        └──────────────┘
       │ Docker SDK / libvirt + asyncssh
       ▼
┌──────────────────────────────┐  ┌──────────────────────────────┐
│  Docker Containers           │  │  QEMU/KVM Virtual Machines   │
│  (Ubuntu 22.04 + agents)     │  │  (Ubuntu 22.04 + agents)     │
│  - Fast startup              │  │  - Full VM isolation          │
│  - Lightweight               │  │  - Snapshot / clone support   │
└──────────────────────────────┘  └──────────────────────────────┘
```

### Key design principles

1. **Single business-logic layer** — `runner/src/service.py` contains all
   container orchestration logic. The WebSocket interface delegates to it.
   Never duplicate business logic in an interface.

2. **Agent definitions in the backend** — agent knowledge (configure commands,
   run command templates) is stored as DB records (`AgentDefinition` +
   `AgentCommand` models). The backend builds and sends fully resolved
   commands to runners. Runners have no agent-specific logic.

3. **Two runner abstraction layers** — each with an ABC and concrete implementation:
   - **Runtime** (`runtime/base.py`) — virtualisation backend. Two implementations:
     Docker (`docker_runtime.py`) and QEMU/KVM (`qemu_runtime.py`). Both can
     run simultaneously — the runner maintains a `dict[str, RuntimeBackend]`
     and selects the correct one per workspace based on `runtime_type`.
   - **Interface** (`interfaces/base.py`) — control plane connection.
     WebSocket client is the only interface (CLI was removed).

3. **Async-first** — the runner uses `asyncio` throughout. The Docker SDK is
   synchronous and wrapped via `asyncio.to_thread()`.

4. **Runner-initiated connections** — runners connect *to* the backend (not the
   other way around). Authentication uses a Bearer API token.

5. **No runner-local database** — the runner has no SQLite database. All
   workspace state is derived from the Docker runtime (container labels and
   status) and cached in memory. The backend stores persistent state and
   reconciles it with runtime state via periodic heartbeats.

6. **Single workspace-ID** — the backend assigns the workspace UUID and sends
   it to the runner in `task:create_workspace`. Both sides use the same ID.

---

## 3. Repository Structure

```
opencuria/
├── AGENTS.md                      ← You are here
├── README.md
├── compose.yml                    ← Default local all-in-one deploy
├── compose.server.yml             ← Distributed backend + webapp deploy
├── .env.example                   ← Local all-in-one environment template
├── .env.server.example            ← Distributed/server environment template
├── .github/workflows/
│   └── runner-build.yml           ← CI/CD: build backend, webapp, runner, workspace images
│
├── runner/                        ← Dumb workspace executor
│   ├── Dockerfile                 ← Workspace container image (Ubuntu 22.04)
│   ├── Dockerfile.runner          ← Runner daemon container image
│   ├── compose.yml                ← Standalone Docker runner deployment
│   ├── systemd/
│   │   ├── opencuria-runner.service ← Native Linux runner unit template (for QEMU hosts)
│   │   └── install-runner-service.sh ← Installs the unit with the current repo path + runner/.env
│   ├── git-wrapper.sh             ← Blocks commits/pushes to main/master
│   ├── workspace-entrypoint.sh    ← Configures git auth inside containers
│   ├── requirements.txt           ← Python dependencies (pip)
│   ├── .env.example               ← Environment variable template
│   │
│   ├── packer/                    ← QEMU/KVM base image builder
│   │   ├── workspace.pkr.hcl     ← Packer template (QCOW2 output)
│   │   ├── init.sh               ← Provisioning script (mirrors Dockerfile)
│   │   └── http/                  ← cloud-init seed files
│   │
│   └── src/                       ← Python application
│       ├── __init__.py
│       ├── __main__.py            ← `python -m src` entry point
│       ├── main.py                ← Typer app, logging setup, serve command
│       ├── config.py              ← pydantic-settings
│       ├── models.py              ← Dataclasses (WorkspaceInfo)
│       ├── service.py             ← Command execution (WorkspaceService)
│       │
│       ├── runtime/               ← Virtualisation abstraction
│       │   ├── base.py            ← ABC: RuntimeBackend
│       │   ├── docker_runtime.py  ← Docker SDK implementation
│       │   └── qemu_runtime.py   ← QEMU/KVM + libvirt + asyncssh
│       │
│       └── interfaces/            ← Control plane abstraction
│           ├── base.py            ← ABC: Interface
│           └── websocket.py       ← python-socketio async client
│
├── backend/                       ← Django control plane
│   ├── Dockerfile                 ← Production Docker image
│   ├── entrypoint.sh              ← Container startup (migrate + serve)
│   ├── manage.py                  ← Django management CLI
│   ├── requirements.txt           ← Python dependencies (pip)
│   ├── pyproject.toml             ← ruff + pytest config
│   ├── .env.example               ← Environment variable template
│   │
│   ├── config/                    ← Django project config
│   │   ├── asgi.py                ← ASGI app (routes WS to socketio)
│   │   ├── urls.py                ← URL routing (Django Ninja API)
│   │   └── settings/              ← Split settings
│   │       ├── base.py            ← Shared settings
│   │       ├── development.py     ← SQLite, DEBUG=True
│   │       └── production.py      ← PostgreSQL, Redis, security
│   │
│   ├── common/                    ← Shared utilities
│   │   ├── exceptions.py          ← Base exception hierarchy
│   │   ├── utils.py               ← UUID gen, token hashing
│   │   └── middleware.py          ← Request logging middleware
│   │
│   └── apps/                      ← Django applications
│       ├── accounts/              ← Custom User model (placeholder)
│       ├── organizations/         ← Organization management (placeholder)
│       ├── credentials/           ← Credential management (env vars + SSH keys)
│       │   ├── enums.py           ← CredentialType (env, ssh_key)
│       │   ├── models.py          ← CredentialService, Credential
│       │   ├── repositories.py    ← Data access layer
│       │   ├── services.py        ← Business logic (keypair gen, resolution)
│       │   ├── schemas.py         ← Pydantic v2 schemas
│       │   └── api.py             ← REST endpoints
│       └── runners/               ← Runner + workspace + agent management
│           ├── enums.py            ← Status enums (TextChoices)
│           ├── models.py           ← Runner, Workspace, Session, Task, AgentDefinition, AgentCommand
│           ├── repositories.py     ← Data access layer (static methods)
│           ├── services.py         ← Business logic (RunnerService)
│           ├── schemas.py          ← Pydantic v2 request/response schemas
│           ├── api.py              ← Django Ninja REST endpoints
│           ├── sio_server.py       ← Socket.IO server (runner comms)
│           ├── exceptions.py       ← Domain-specific exceptions
│           └── tests/              ← pytest test suite
│
└── webapp/                        ← Vue 3 frontend SPA
    ├── Dockerfile                 ← Production Docker image (multi-stage: build + nginx)
    ├── nginx.conf                 ← nginx config for SPA serving
    ├── entrypoint.sh              ← Runtime config injection
    ├── package.json               ← Node.js dependencies
    └── src/
        ├── services/config.ts     ← Runtime config loader (/config.json)
        ├── services/api.ts        ← REST API client
        └── services/socket.ts     ← Socket.IO client
```

---

## 4. Runner — Detailed Architecture

### 4.1 Configuration (`config.py`)

All settings are loaded from environment variables with prefix `RUNNER_` and
optionally from a `.env` file. See `.env.example` for all available options.

Key settings: `RUNNER_BACKEND_URL`, `RUNNER_API_TOKEN`,
`RUNNER_DOCKER_NETWORK`, `RUNNER_HEARTBEAT_INTERVAL`, `RUNNER_LOG_LEVEL`,
`RUNNER_LOG_FORMAT`, `RUNNER_ENABLED_RUNTIMES`.

### 4.2 Data Model (`models.py`)

The runner uses lightweight in-memory dataclasses instead of a database:

| Dataclass | Purpose |
|-----------|--------|
| `WorkspaceInfo` | Maps a workspace UUID to a runtime instance. Holds `workspace_id`, `instance_id`, `agent_type`, `runtime_type`, `status`, `repos`, `created_at`. |

State is derived from the runtime backends (single point of truth) via
labels (Docker) or libvirt domain names (QEMU) and cached in memory.
The in-memory cache is rebuilt on startup via `sync_from_runtime()`.

### 4.3 Runtime Layer (`runtime/`)

`RuntimeBackend` (ABC) defines: `create_workspace`, `stop_workspace`,
`start_workspace`, `remove_workspace`, `workspace_exists`,
`get_workspace_status`, `exec_command` (streaming), `exec_command_wait`.
Optional snapshot methods: `snapshot_workspace`, `list_snapshots`,
`delete_snapshot`, `clone_workspace`.

**DockerRuntime** — implements the ABC using the `docker` Python SDK. All blocking
SDK calls are wrapped in `asyncio.to_thread()`. Streaming output uses an
`asyncio.Queue` bridge between the sync Docker stream and async consumers.
Container naming: `opencuria-workspace-{uuid}`, Volume naming: `opencuria-workspace-{uuid}`.

**QemuRuntime** — implements the ABC using `libvirt` for VM lifecycle and
`asyncssh` for command execution via SSH. Key features:
- QCOW2 COW overlays backed by an explicit image definition build or captured image
- Custom image builds resolve Ubuntu cloud images per requested release
  (for example `ubuntu:24.04` downloads/caches its own build source image)
- cloud-init ISO for SSH key injection and environment variable setup
- Snapshot support: `qemu-img convert` for consistent snapshots,
  `fsFreeze`/`fsThaw` for live VMs
- Clone: creates a new overlay backed by a snapshot image
- Domain naming: `opencuria-{uuid}`
- Disk path: `{disk_dir}/{uuid}.qcow2`
- Custom QEMU image builds are stored under `/var/lib/opencuria/base-images/`;
  Ubuntu cloud image downloads are cached under `/var/lib/opencuria/images/`.
  Both directories must be writable by the runner user and traversable by
  libvirt-qemu.

Both runtimes can run simultaneously — the runner maintains a
`dict[str, RuntimeBackend]` and selects per workspace based on `runtime_type`.

### 4.4 Runner as Dumb Executor

The runner has **no agent-specific logic** and does not advertise per-agent
capabilities. All agent knowledge (commands, templates) is owned by the backend
and sent to the runner as structured command dicts.

The runner executes two kinds of commands from the backend:
- **Configure commands** — sent with `task:create_workspace`, executed
  sequentially after container creation and repo cloning.
- **Run command** — sent with `task:run_prompt`, executed with streaming output.

### 4.5 Service Layer (`service.py`)

`WorkspaceService` is the **only** place where container orchestration logic lives:
- `sync_from_runtime()` — rebuilds the in-memory cache from Docker containers
- `create_workspace(repos, agent_type, env_vars, ssh_keys, configure_commands, workspace_id) -> UUID`
- `run_command(workspace_id, command) -> AsyncIterator[str]`
- `stop_workspace(workspace_id)`
- `resume_workspace(workspace_id)`
- `remove_workspace(workspace_id)`
- `list_workspaces() -> list[WorkspaceInfo]`
- `get_workspace(workspace_id) -> WorkspaceInfo`
- `get_workspace_statuses() -> list[dict]` — lightweight for heartbeats
- `start_terminal(workspace_id, cols, rows) -> terminal_id` — opens interactive PTY
- `read_terminal(terminal_id) -> AsyncIterator[bytes]` — streams PTY output
- `write_terminal(terminal_id, data)` — sends stdin to PTY
- `resize_terminal(terminal_id, cols, rows)` — resizes PTY
- `close_terminal(terminal_id)` — closes PTY session

### 4.6 Interfaces

**WebSocket** (`interfaces/websocket.py`): python-socketio `AsyncClient`.
Connects to the Django backend, authenticates with Bearer token. Sends
`supported_runtimes` on register. Listens for task events
(`task:create_workspace`, `task:run_prompt`, etc.) and emits results
(`workspace:created`, `output:chunk`, `output:complete`, etc.). Each task runs
as an `asyncio.Task` for concurrent execution.

The runner has no CLI interface — it operates purely as a daemon.

### 4.7 Workspace Container (`Dockerfile`)

Based on Ubuntu 22.04. Pre-installed:
- Node.js 22.x, GitHub CLI, GitHub Copilot CLI (`@github/copilot`)
- Python 3, pip, venv, git, openssh-client, build-essential, common tools
- `git-wrapper.sh` — safety wrapper that blocks commits/pushes to `main`/`master`
- `workspace-entrypoint.sh` — lightweight entrypoint that executes the container command

The container runs with `tail -f /dev/null` (stays alive) and the runner
executes commands via `docker exec`.

---

## 5. Backend — Detailed Architecture

### 5.1 Tech Stack

- **Django 5.x** — web framework
- **Django Ninja** — REST API with auto-generated OpenAPI docs at `/api/v1/docs`
- **Django Channels + Daphne** — ASGI server for WebSocket support
- **python-socketio** — Socket.IO server for runner communication
- **Pydantic v2** — request/response schemas
- **pydantic-settings** — environment-based configuration
- **structlog** — structured logging
- **SQLite** (dev) / **PostgreSQL** (prod) — database

### 5.2 Architecture Layers

The backend follows **Clean Architecture** with strict separation of concerns:

1. **API Layer** (`api.py`) — thin Django Ninja routers. No business logic.
   Validates input via Pydantic schemas, delegates to services, returns responses.
2. **Service Layer** (`services.py`) — all business logic lives here.
   `RunnerService` orchestrates runner management, workspace lifecycle, and
   prompt dispatch. Receives a `sio_server` instance for emitting events.
3. **Repository Layer** (`repositories.py`) — data access via static methods
   wrapping Django ORM. Services never call the ORM directly.
4. **Socket.IO Layer** (`sio_server.py`) — event handlers for runner WebSocket
   connections. Thin adapter that delegates to `RunnerService`.

### 5.3 Data Model (`apps/runners/models.py`)

| Model | Purpose |
|-------|--------|
| `Runner` | Registered runner instance. Tracks name, hashed API token, available agents, available runtimes, online/offline status, Socket.IO session ID. |
| `Workspace` | A workspace on a runner. FK to Runner. Tracks status (`creating` → `running` → `stopped` / `failed`), agent type, runtime type (`docker`/`qemu`), repos (JSON). |
| `Session` | A prompt/response pair within a workspace. Tracks prompt, accumulated output, status, timestamps. |
| `Task` | A dispatched command to a runner. FK to Runner, optional FK to Workspace/Session. Tracks type, status, error message. |
| `AgentDefinition` | DB-managed agent type (e.g. "copilot"). Unique name, description. |
| `AgentCommand` | A command belonging to an agent. Phase (`configure`/`run`), args (JSON list with `{prompt}`/`{workdir}` placeholders), workdir, env, execution order. |
| `Snapshot` | A snapshot of a QEMU workspace. FK to Workspace. Tracks runner_snapshot_id, name, size_bytes, created_at. |

**Credentials models** (`apps/credentials/models.py`):

| Model | Purpose |
|-------|--------|
| `CredentialService` | Admin-managed catalog entry. Has `credential_type` (`env` or `ssh_key`), `env_var_name` (for env type), `label`. |
| `Credential` | Org-scoped instance. FK to `CredentialService`. Stores `encrypted_value` (Fernet) and `public_key` (OpenSSH, SSH keys only). |

### 5.4 REST API (`/api/v1/`)

Six separate routers:

| Prefix | Endpoints |
|--------|----------|
| `/api/v1/runners/` | `GET /` list, `POST /` register (returns API token), `GET /{id}/` detail |
| `/api/v1/workspaces/` | `GET /` list, `POST /` create, `GET /{id}/` detail, `DELETE /{id}/` remove, `POST /{id}/prompt/`, `POST /{id}/stop/`, `POST /{id}/resume/`, `GET /{id}/sessions/`, snapshots (below) |
| `/api/v1/workspaces/{id}/snapshots/` | `GET /` list, `POST /` create, `DELETE /{snapshot_id}/` delete, `POST /{snapshot_id}/clone/` clone |
| `/api/v1/agents/` | `GET /` list available agents across online runners |
| `/api/v1/credential-services/` | `GET /` list catalog (admin-managed) |
| `/api/v1/credentials/` | `GET /` list, `POST /` create, `PATCH /{id}/`, `DELETE /{id}/`, `GET /{id}/public-key/` |

**Parity requirement:** Every capability exposed via the REST API must also be
available via MCP. When adding a new REST endpoint, add the corresponding MCP
tool/operation in the same change.

**API key permission requirement:** New endpoints must come with explicit
fine-grained API key permissions that can be assigned in API key management.
Existing API keys must not automatically receive newly introduced permissions.

### 5.5 Socket.IO Server (`sio_server.py`)

The backend hosts a Socket.IO `AsyncServer` at `/ws/runner`. Runners connect
with `Authorization: Bearer <token>` and communicate via the event protocol
defined in Section 7.

The ASGI routing in `config/asgi.py` directs `/ws/runner` and `/socket.io`
paths to the Socket.IO app; all other requests go to Django.

### 5.6 Settings (`config/settings/`)

Settings are split into `base.py` (shared), `development.py` (SQLite, debug),
and `production.py` (PostgreSQL, Redis, security headers). The `__init__.py`
auto-selects based on `DJANGO_ENV` environment variable (default: `development`).

### 5.7 Placeholder Apps

- **accounts** — Custom `User(AbstractUser)` model. Set as `AUTH_USER_MODEL`
  from the start so migrations don't need to be reset later.
- **organizations** — Empty placeholder for future multi-tenancy.

### 5.8 Credentials App

Org-scoped credentials can be attached to workspaces at creation time.
Two types are supported:

- **`env`** — injected as environment variables (`env_var_name=value`) inside the container.
- **`ssh_key`** — an Ed25519 keypair auto-generated on creation (`generate_ssh_keypair()` in `common/utils.py`). The private key is Fernet-encrypted; the OpenSSH public key is stored in plain text and exposed via `GET /credentials/{id}/public-key/`.

`CredentialService` records are admin-managed catalog entries (one type per service). `Credential` records are org-scoped instances with an encrypted value.

When a workspace is created, `CredentialSvc.resolve_credentials()` decrypts all attached credentials and returns a `ResolvedCredentials` dataclass with `env_vars: dict` and `ssh_keys: list[str]`. The backend sends both to the runner in the `task:create_workspace` payload. The runner's `_setup_ssh_keys()` writes each private key to `/root/.ssh/`, runs `ssh-keyscan` for common Git providers, and configures `~/.ssh/config` **before** cloning repos.

---

## 6. Coding Rules

### 6.1 General

- **Language**: All code, comments, docstrings, commit messages, and
  documentation in **English**.
- **Python version**: 3.10+ (use modern syntax: `X | Y` unions, `match`
  statements where appropriate).
- **Type hints**: Required on all function signatures. Use `from __future__
  import annotations` for forward references.
- **Docstrings**: Required on all public classes and functions. Use
  triple-quoted strings with a one-line summary, optional blank line, then
  detail.

### 6.2 Python Style

- Follow **PEP 8** with a line length of **88 characters** (Black default).
- Use `snake_case` for functions, methods, variables, modules.
- Use `PascalCase` for classes.
- Use `UPPER_SNAKE_CASE` for module-level constants.
- Prefer `dataclass` / `NamedTuple` / `pydantic.BaseModel` over plain dicts
  for structured data.
- Imports: stdlib, then third-party, then local — separated by blank lines.
  Use relative imports within the `src` package (e.g. `from .config import ...`).

### 6.3 Async Conventions

- All I/O-bound operations must be `async`.
- Sync blocking calls (e.g. Docker SDK) must be wrapped in
  `asyncio.to_thread()`.
- Use `asyncio.Queue` to bridge sync iterators with async consumers (see
  `DockerRuntime.exec_command`).
- Never use `time.sleep()` — use `asyncio.sleep()`.

### 6.4 Error Handling

- Raise descriptive `ValueError` / `RuntimeError` for invalid states.
- Log exceptions with `structlog` using `log.exception(...)`.
- Service methods should catch errors and update DB state (e.g. mark sessions
  as `FAILED`) before re-raising.

### 6.5 Adding a New Agent

1. Create a new `AgentDefinition` record in the database (via Django Admin,
   management command, or data migration)
2. Add `AgentCommand` records for the configure phase (0+) and run phase (exactly 1)
3. Use `{prompt}` and `{workdir}` placeholders in command args / workdir
4. Update the workspace `Dockerfile` if the agent needs additional tooling

### 6.6 Adding a New Runtime Backend

1. Create `runner/src/runtime/<name>.py`
2. Subclass `RuntimeBackend` from `runtime/base.py`
3. Implement all abstract methods
4. Wire it up in `main.py` / `cli.py` (replace `DockerRuntime` instantiation)

### 6.7 Dependencies

- Managed via `requirements.txt` with version pins (e.g. `>=7.0,<8.0`).
- Core dependencies: `docker`, `python-socketio[asyncio_client]`,
  `pydantic-settings`, `structlog`, `typer`, `rich`.
- Do not add dependencies without a clear justification.

### 6.8 Logging

- Use `structlog` exclusively — never `print()` for operational output.
- Bind context (workspace_id, agent, task_id) early and let it propagate.
- Use `log_format=console` for development, `log_format=json` for production.

### 6.9 Frontend UI Component Consistency

- In the `webapp`, always use the shared standard UI components from
  `webapp/src/components/ui` for interactive UI elements.
- This is mandatory for buttons, dialogs/modals, inputs, textareas, selects and
  similar controls. Do not introduce ad-hoc native elements when a standard
  component exists (`UiButton`, `UiDialog`, `UiInput`, `UiTextarea`, etc.).
- Organization Settings and all future views must follow this rule so the
  Liquid Glass design is applied consistently across the app.

### 6.10 Mandatory Test Strategy (strict)

All AI agents must follow these testing rules for every code change. This is
mandatory and not optional.

#### Test frameworks by component

- **backend/**: `pytest` (with `pytest-django` and `pytest-asyncio`)
- **runner/**: `pytest` only (do not add new unittest-only test modules)
- **webapp/**: `vitest`

#### Required test command matrix

Before finishing work, run the component-specific commands affected by your
change:

- `cd backend && source .venv/bin/activate && pytest -q`
- `cd runner && source .venv/bin/activate && pytest -q`
- `cd webapp && npm run test`

If your change touches multiple components, run all relevant commands.

#### Required test scope per change

For each behavior change, include or update tests that cover:

1. happy path behavior
2. permission/authentication constraints (if applicable)
3. at least one relevant failure or edge path

Do not merge behavior changes that only rely on manual testing.

#### Test quality guardrails

- Replace outdated tests when API contracts changed (do not preserve stale assertions).
- Keep tests deterministic (no flaky timing assumptions, no real external network calls).
- Prefer service-level and API-contract tests for backend changes.
- Prefer unit tests for frontend logic and rendering behavior.
- Use shared fixtures/helpers when patterns repeat across tests.

#### Ownership expectation for agents

When an AI agent modifies code, it must also maintain the corresponding tests in
the same change so the suite remains green and current.

### 6.11 Mandatory Pre-Commit Gate

- Run `./scripts/setup-dev.sh` on a fresh clone before starting development.
- The mandatory commit gate is `./.githooks/pre-commit`.
- Never bypass this gate. A commit is only allowed immediately after the script
  prints `Ready to commit.`
- The gate checks the full test suite, forbids direct commits to `main`, and
  requires the current branch to include the latest `origin/main`.
- Direct commits to `main` are forbidden.
- If any check fails, fix it, rerun the gate, and do not commit until it passes.

---

## 7. Running the Runner

For development, both the backend and the runner each have their own Python virtual environments (.venv); these must be activated once before any Python-related operations.

The runner requires system build tools to compile `libvirt-python`. On Debian/Ubuntu, install these before running `pip install`:

```bash
sudo apt-get update
sudo apt-get install -y build-essential python3-dev pkg-config libvirt-dev
```

```bash
cd runner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Daemon mode (connects to backend) — the only way to run the runner
export RUNNER_API_TOKEN=<token>
export RUNNER_BACKEND_URL=ws://localhost:8000/ws/runner/
python -m src serve
```

---

## 8. Running the Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Apply migrations
python manage.py migrate

# Run development server (Daphne ASGI)
python manage.py runserver

# API docs available at http://localhost:8000/api/v1/docs
```

### Dev login credentials

Use the values configured via `LOCAL_ADMIN_EMAIL` / `LOCAL_ADMIN_PASSWORD`
for local all-in-one setups. Do not assume fixed default credentials.

Frontend runs on `http://localhost:5173` (proxies `/api` and `/ws` to the backend on port 8000).

Environment variables (optional, see `.env.example`):
- `DJANGO_ENV` — `development` (default) or `production`
- `DJANGO_SECRET_KEY` — secret key (auto-generated in dev)
- `DJANGO_DEBUG` — `True`/`False`
- `DATABASE_URL` — PostgreSQL URL (production only)
- `REDIS_URL` — Redis URL for channel layer (production only)

---

## 9. Backend <-> Runner Protocol (WebSocket)

The runner connects to the backend as a socketio client. Events:

| Direction | Event | Payload |
|-----------|-------|---------|
| Runner -> Backend | `runner:register` | `{supported_runtimes: ["docker", "qemu"], status}` |
| Runner -> Backend | `runner:heartbeat` | `{workspaces: [{workspace_id, status, agent_type}]}` |
| Backend -> Runner | `task:create_workspace` | `{task_id, workspace_id, repos, agent_type, env_vars, configure_commands: [{args, workdir, env, description}]}` |
| Runner -> Backend | `workspace:created` | `{task_id, workspace_id, status}` |
| Backend -> Runner | `task:run_prompt` | `{task_id, workspace_id, prompt, command: {args, workdir, env, description}}` |
| Runner -> Backend | `output:chunk` | `{task_id, workspace_id, line}` |
| Runner -> Backend | `output:complete` | `{task_id, workspace_id}` |
| Backend -> Runner | `task:stop_workspace` | `{task_id, workspace_id}` |
| Runner -> Backend | `workspace:stopped` | `{task_id, workspace_id}` |
| Backend -> Runner | `task:resume_workspace` | `{task_id, workspace_id}` |
| Runner -> Backend | `workspace:resumed` | `{task_id, workspace_id}` |
| Backend -> Runner | `task:remove_workspace` | `{task_id, workspace_id}` |
| Runner -> Backend | `workspace:removed` | `{task_id, workspace_id}` |
| Runner -> Backend | `workspace:error` | `{task_id, error}` |
| Runner -> Backend | `output:error` | `{task_id, workspace_id, error}` |
| Backend -> Runner | `task:start_terminal` | `{task_id, workspace_id, cols, rows}` |
| Runner -> Backend | `terminal:started` | `{task_id, workspace_id, terminal_id}` |
| Runner -> Backend | `terminal:output` | `{workspace_id, terminal_id, data}` (base64) |
| Backend -> Runner | `terminal:input` | `{workspace_id, terminal_id, data}` (base64) |
| Backend -> Runner | `terminal:resize` | `{workspace_id, terminal_id, cols, rows}` |
| Backend -> Runner | `terminal:close` | `{workspace_id, terminal_id}` |
| Runner -> Backend | `terminal:closed` | `{workspace_id, terminal_id}` |
| Backend -> Runner | `task:snapshot_workspace` | `{task_id, workspace_id, snapshot_name}` |
| Runner -> Backend | `snapshot:created` | `{task_id, workspace_id, snapshot_id, name, size_bytes}` |
| Backend -> Runner | `task:delete_snapshot` | `{task_id, workspace_id, snapshot_id}` |
| Runner -> Backend | `snapshot:deleted` | `{task_id, workspace_id, snapshot_id}` |
| Backend -> Runner | `task:clone_workspace` | `{task_id, workspace_id, source_snapshot_id, ...}` |

Frontend ↔ Backend events (via `/frontend` Socket.IO namespace):

| Direction | Event | Payload |
|-----------|-------|---------|
| Frontend -> Backend | `frontend:subscribe_workspace` | `{workspace_id}` |
| Frontend -> Backend | `frontend:unsubscribe_workspace` | `{workspace_id}` |
| Frontend -> Backend | `frontend:terminal_input` | `{workspace_id, terminal_id, data}` (base64) |
| Frontend -> Backend | `frontend:terminal_resize` | `{workspace_id, terminal_id, cols, rows}` |
| Frontend -> Backend | `frontend:terminal_close` | `{workspace_id, terminal_id}` |
| Backend -> Frontend | `terminal:started` | `{workspace_id, terminal_id, task_id}` |
| Backend -> Frontend | `terminal:output` | `{workspace_id, terminal_id, data}` (base64) |
| Backend -> Frontend | `terminal:closed` | `{workspace_id, terminal_id}` |

Authentication: `Authorization: Bearer <RUNNER_API_TOKEN>` header on connect.
Frontend Socket.IO authentication: JWT token passed in `auth: { token }` on connect (validated server-side).

---

## 10. Production Deployment

### Architecture

```
Local all-in-one:
127.0.0.1:8080  ──►  webapp
127.0.0.1:8000  ──►  backend
runner (same compose)  ──WS──►  backend

Distributed:
webapp public URL  ──►  webapp container
backend public URL ──►  backend container
Runner (any machine) ──WS/WSS──► backend
```

### Docker Compose

Repo root defaults to the local all-in-one stack:

- `compose.yml` runs **backend** (Django development mode + SQLite),
  **webapp**, a one-shot **bootstrap** service, and a local Docker **runner**.
- It is intended for `docker compose up -d` on Linux, macOS, and Windows.
- The bootstrap command auto-creates the default admin user, organization,
  runner record, and a default Docker workspace image artifact.

Distributed/server deployments use `compose.server.yml`:

- **db** (PostgreSQL 16)
- **redis** (Redis 7)
- **backend** (Django/Daphne production mode)
- **webapp** (Vue 3/nginx)

Standalone Docker runners use `runner/compose.yml`.
Native Linux runner hosts that need QEMU/KVM should use the systemd unit at
`runner/systemd/opencuria-runner.service` via
`runner/systemd/install-runner-service.sh` instead of containerizing libvirt.
The default `RUNNER_QEMU_SSH_USER` is `root`; existing runner env files that
still pin `ubuntu` should be migrated to `root` because the shipped QEMU desktop
stack uses `/root` paths.

### Runner CI/CD

`.github/workflows/runner-build.yml` builds and publishes multi-arch images for:
- `ghcr.io/opencuria/backend`
- `ghcr.io/opencuria/webapp`
- `ghcr.io/opencuria/runner`
- `ghcr.io/opencuria/workspace`

It runs for pushes to `main`, `release`, tags, and manual dispatches.

### Security measures

- **Local mode favors fast startup** — single compose stack, SQLite, auto-bootstrap
- **Distributed mode supports HTTPS/WSS** behind your preferred reverse proxy or ingress
- **JWT auth** on REST API (global `JWTAuth` on Django Ninja) and on Socket.IO `/frontend` namespace
- **Bearer token auth** on Socket.IO default namespace (runner connections)
- **CORS** restricted to frontend origin in production
- **Secure cookies** (`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`)
- **Proxy SSL header** (`SECURE_PROXY_SSL_HEADER`) for correct `request.is_secure()` behind nginx
- **Fernet encryption** for stored credentials (`CREDENTIAL_ENCRYPTION_KEY`)

---

## 11. Future Work

- [ ] **Task queue**: Celery or Django-Q for async task processing
- [ ] **Alternative runtimes**: Firecracker, Kata Containers
- [ ] **Workspace persistence**: Snapshot / restore workflows
- [ ] **Metrics & observability**: Prometheus metrics, structured log shipping
