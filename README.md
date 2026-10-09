<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./brand/opencuria-logo-dark.svg" />
    <img src="./brand/opencuria-logo.svg" alt="OpenCuria" width="320" />
  </picture>
</p>

<p align="center">
  <strong>Run and manage AI coding agents from one place.</strong>
</p>

<p align="center">
  OpenCuria gives you a central backend, a web dashboard, and runners that launch Docker workspaces or QEMU VMs.
</p>

<p align="center">
  🐳 Local all-in-one &nbsp;•&nbsp; 🌍 Distributed deployment &nbsp;•&nbsp; 🤖 Multiple agents &nbsp;•&nbsp; 🧱 Docker and QEMU runtimes
</p>

---

## ✨ Quick Start

Want the whole stack locally with one command?

```bash
cp .env.example .env
docker compose up -d
```

Then open:

- 🌐 Web app: `http://127.0.0.1:8080`
- 🔌 API: `http://127.0.0.1:8000/api/v1`
- 📚 API docs: `http://127.0.0.1:8000/api/v1/docs`

Local login credentials come from your `.env` values:

- Email: `LOCAL_ADMIN_EMAIL`
- Password: `LOCAL_ADMIN_PASSWORD`

What this starts for you:

- `backend` in local mode
- `webapp` on localhost
- `runner` on the same machine
- automatic bootstrap for admin, org, runner, and default workspace image

No manual runner registration is needed in local mode.

> [!NOTE]
> The one-command local path is intentionally **Docker-only**. If you want
> QEMU/KVM workspaces, keep using the local backend/webapp, but run a
> **native Linux runner** with `RUNNER_ENABLED_RUNTIMES=qemu` or
> `RUNNER_ENABLED_RUNTIMES=docker,qemu`.

## 🧭 What OpenCuria Is

OpenCuria is a control plane for AI coding agents.

- `backend/` is the Django API and runner control plane
- `webapp/` is the dashboard
- `runner/` executes Docker workspaces or QEMU VMs

Use it when you want:

- one local stack for fast onboarding
- a central server with one or more remote runners
- isolated agent workspaces on Docker or QEMU

## 🐳 Local All-in-One

The repository root is optimized for the fast path:

```bash
docker compose up -d
```

Key files:

- [`compose.yml`](./compose.yml) — local default
- [`.env.example`](./.env.example) — local environment template

Local mode is designed for:

- Linux
- macOS with Docker Desktop
- Windows with Docker Desktop

## 🌍 Distributed Setup

If you want a proper split setup, run the central services and the runners separately.

### 1. Central server

```bash
cp .env.server.example .env
docker compose -f compose.server.yml up -d
```

Key files:

- [`compose.server.yml`](./compose.server.yml)
- [`.env.server.example`](./.env.server.example)

This starts:

- PostgreSQL
- Redis
- backend
- webapp

### 2. Remote Docker runner

```bash
cd runner
cp .env.example .env
docker compose up -d
```

Key files:

- [`runner/compose.yml`](./runner/compose.yml)
- [`runner/.env.example`](./runner/.env.example)

For Docker-only runner hosts, leave `RUNNER_QEMU_SSH_USER` at its default `root`
unless you are intentionally customizing the QEMU guest image to use a different
SSH login user.

### 3. Remote QEMU runner

If a runner host should support QEMU/KVM, run it natively on Linux instead of inside a container.

Use:

- [`runner/systemd/opencuria-runner.service`](./runner/systemd/opencuria-runner.service)
- [`runner/.env.example`](./runner/.env.example)

The default QEMU SSH user is `root`. This matches the shipped QEMU cloud-init
and desktop setup, which provision and start the desktop session under `/root`.

Why native for QEMU:

- better libvirt/KVM integration
- simpler disk and snapshot permissions
- less operational overhead than containerized libvirt

Recommended setup flow:

1. Start the central services first (`compose.server.yml` or a local source setup
   for backend + webapp).
2. Create a runner API token in the backend UI/API.
3. On the Linux runner host, install the native QEMU/libvirt dependencies plus
   the Python build dependencies required by `libvirt-python`:

   ```bash
   sudo apt-get update
   sudo apt-get install -y \
     qemu-kvm \
     libvirt-daemon-system \
     libvirt-clients \
     genisoimage \
     build-essential \
     python3-dev \
     pkg-config \
     libvirt-dev
   ```

4. Clone this repository onto the runner host, create the runner virtualenv,
   and install dependencies:

   ```bash
   cd runner
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. Copy `runner/.env.example` to `runner/.env` and set at least:

   ```dotenv
   RUNNER_API_TOKEN=...
   RUNNER_BACKEND_URL=http://<backend-host>:8000
   RUNNER_ENABLED_RUNTIMES=qemu
   # or: RUNNER_ENABLED_RUNTIMES=docker,qemu
   ```

6. Create the QEMU storage directories configured in that env file, for example:

   ```bash
   sudo install -d -m 755 \
     /var/lib/opencuria/images \
     /var/lib/opencuria/disks \
     /var/lib/opencuria/snapshots
   ```

7. Install the systemd unit from the repo. The helper script renders the unit
   with the real checkout path and keeps using `runner/.env` directly:

   ```bash
   sudo ./runner/systemd/install-runner-service.sh
   sudo systemctl enable --now opencuria-runner
   ```

8. Follow the logs and confirm the runner shows up online in the backend:

   ```bash
   sudo journalctl -u opencuria-runner -f
   ```

QEMU image definitions currently require `ubuntu:<version>` as the base distro.
When a QEMU image build is triggered, the runner downloads the matching Ubuntu
cloud image into `RUNNER_QEMU_IMAGE_CACHE_DIR` on first use.

## 📦 Images

Published via GHCR:

- `ghcr.io/opencuria/backend`
- `ghcr.io/opencuria/webapp`
- `ghcr.io/opencuria/runner`
- `ghcr.io/opencuria/workspace`

The GitHub Actions workflow builds multi-arch images for:

- `main`
- `release`
- tags

## 🛠️ Develop From Source

If you do not want Docker Compose for day-to-day development:

```bash
./scripts/setup-dev.sh
```

Optional local workspace image build:

```bash
./scripts/setup-dev.sh --build-workspace-image
```

After that you can run `backend`, `webapp`, and `runner` individually. When
updating a source checkout, apply backend migrations before starting the ASGI server:

```bash
cd backend
.venv/bin/python manage.py migrate --noinput
.venv/bin/uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --workers 1 --lifespan on --ws websockets-sansio --ws-max-size 209715200
```

The Docker backend entrypoint runs migrations automatically and starts one Uvicorn
ASGI worker. The worker owns the in-process scheduled-task loop alongside Socket.IO
and the harness; keep it single-process/single-worker. A PostgreSQL advisory lock
rejects duplicate workers before recovery starts; SQLite uses a renewed TTL-row lease
(best effort, not suitable for separate hosts without shared locking semantics).
Raw Django `runserver` has no scheduler startup hook and is not suitable for production.
Scheduled tasks are personal
recurring prompts (daily or selected weekdays, local `HH:MM` and an IANA timezone),
with DST gaps skipped and fall-back repeated times executed once. Each occurrence
creates a new root harness session. REST endpoints are under
`/api/v1/scheduled-tasks/` (`harness:read` for listing/history, `harness:run` for
create/edit/pause/delete/run-now); matching MCP tools are exposed.

For a local Linux setup with backend/webapp from source and a native QEMU
runner on the same machine, use the same QEMU runner flow above, but point
`RUNNER_BACKEND_URL` at your local backend and run the runner in the foreground
with `python -m src` while iterating.

## Image versions

Captured images and image-definition builds share the same version model.
Each capture or build is a standalone version (`v1`, `v2`, …) with an optional
message. New workspaces always use **Latest**. Reset rebuilds the same workspace
id on its current version; Update moves it to Latest. Capture replaces the
source workspace with the new version. Name, URL, chats, credentials, plugins
and schedules stay; only the runner disk and runtime are recreated.

**Settings → Workspace policy → Image versions to keep** (default **2**, range
1–20) retains Latest plus the newest ready versions. Unused older versions are
deleted automatically; versions still used by a workspace stay until that
workspace is gone. Raising the setting does not restore already deleted
versions.

REST: `GET/PATCH /api/v1/captured-images/`, `POST /api/v1/workspaces/{id}/recreate/`,
`PATCH /api/v1/organizations/{id}/workspace-policy/`. Matching MCP tools:
`list_captured_images`, `update_captured_image`,
`create_workspace_from_captured_image`, `recreate_workspace`,
`get_workspace_policy`, `update_workspace_policy`.

## Subagent nesting

**Settings → Agents → Subagents → Maximum nesting depth** sets the organization-wide
limit for new runs (default: **2**, minimum: **1**). The main agent is depth 0;
depth 2 allows a subagent to delegate to another subagent, but blocks a third
subagent level. This limits nesting, not the number of parallel tasks. Children
inherit their root run's limit, and resumed tasks retain their persisted depth.
Changes do not alter an active run's limit.

REST: `GET/PUT /api/v1/subagent-config/` with `{"max_depth": 2}`.
Matching MCP tools: `get_subagent_config` and `save_subagent_config`.
Reading requires `harness:read`; saving requires `harness:run` and organization
membership. Run backend migrations before using this setting.

## MCP OAuth integrations

OAuth-capable HTTP MCP plugins (including the seeded Notion plugin) can connect named personal or organization accounts from **Settings → Credentials**. Configure the fixed HTTPS callback and frontend return URLs on the backend before enabling this in production. See [MCP OAuth setup and operations](./docs/mcp-oauth.md) for required environment configuration, workspace attachment, security notes, and provider requirements.

## Claude Agent

OpenCuria can run the pinned Claude Code CLI as a separate harness inside a workspace, using your own Anthropic API or authorized subscription token. See the [Claude Agent guide](./docs/claude-agent.md) for setup, runtime details, data handling, and test coverage.

## Managed desktop resources

See [Managed desktop resources](./docs/managed-desktop-resources.md) for plugin policy and rollout, and the [backend](./backend/README.md) and [runner](./runner/README.md) component guides.

## License

This project is licensed under the GNU Affero General Public License v3.0.
See [`LICENSE`](./LICENSE).

### Lifecycle recovery operations

Backend and runner lifecycle protocol versions must match. Keep the runner's
`RUNNER_STATE_DIR` persistent across restarts/upgrades (`runner/compose.yml` mounts
`runner_state`) together with workspace disks/volumes. Do not share one journal
between runners or delete it to resolve a failed task. Run migrations and the
independent `python manage.py recover_lifecycle` worker alongside ASGI (Compose
and the backend systemd worker unit provide this process).

For a systemd source checkout, install the worker and its backend dependency:

```bash
sudo ./backend/systemd/install-recovery-service.sh
```

The installer uses the backend virtualenv and environment file, applies migrations,
enables `opencuria-recover-lifecycle.service` at boot, and starts the backend and
worker together. It briefly stops both services to apply migrations. Override
`--env-file` or the service names when the deployment differs. To inspect the
rendered files first, use `--no-activate --output-dir /tmp/opencuria-systemd`.
When updating a local runner as well, add `--restart-runner opencuria-runner.service`.
The worker reports readiness after its first successful tick and Redis connectivity
check, and renews systemd's 90-second watchdog only after both succeed. Failures
remain visible in
`systemctl status opencuria-recover-lifecycle` and
`journalctl -u opencuria-recover-lifecycle`.

SQLite development deployments use `IMMEDIATE` transactions and a 20-second
busy timeout so concurrent atomic writes wait before reading, instead of failing
during a read-to-write lock upgrade. Idle delivery polling does not reserve the
writer lock. Keep transactions short; PostgreSQL remains the production backend.
See [Django's SQLite transaction guidance](https://docs.djangoproject.com/en/5.2/ref/databases/#transactions-behavior).
Backend tests use a separate temporary SQLite file, so concurrency checks exercise
the same lock waiting as the application rather than shared-cache memory locks.

For an unresolved operation, use `GET /api/v1/runners/operations/` then
`GET /api/v1/runners/operations/{id}/`. Offline inspection returns unknown;
it never clears a fence. `POST .../{id}/reconcile/` applies proven journal
completion; `retry/` is only for known finished failed start/stop/remove, bounded
and requiring fresh resource evidence. Organization admins can explicitly
`acknowledge_interrupted/` only after proof of no executing handler/helper and a
complete current-session runtime scan. Acknowledgment preserves all resources;
partial creation stays failed, with disks intact. Removal is a separate explicit
request. No full create/build/capture rerun or external-stop auto-start occurs.
Equivalent MCP tools: `list_lifecycle_operations`, `inspect_lifecycle_operation`,
`dispose_lifecycle_operation`. Details, authorization and return shapes are in
[Lifecycle implementation](docs/image-lifecycle-implementation.md#recovery-hardening-operator-workflow).
