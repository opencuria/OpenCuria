<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./brand/opencuria-logo-dark.svg" />
    <img src="./brand/opencuria-logo.svg" alt="OpenCuria" width="320" />
  </picture>
</p>

<p align="center">
  <strong>Run and manage AI coding agents from one place.</strong>
</p>

OpenCuria is a control plane for AI coding agents: a central backend, a web
dashboard, a mobile app and runners that execute agents in QEMU/KVM virtual machines.

- `backend/`: Django API, agent harness and runner control plane
- `webapp/`: Vue dashboard
- `mobile/`: native Expo iOS/Android client ([guide](./mobile/README.md))
- `runner/`: executes QEMU/KVM workspaces ([guide](./runner/README.md))
- `packages/`: shared chat rules, API client and the Computer Use runtime

Agents run on the built-in harness, the pinned Claude Code CLI or the pinned Codex
app-server. Workspaces provide a desktop, terminal, Git and files; plugins add
skills, MCP servers and credentials. A built-in OpenCuria MCP server lets agents
manage workspaces, chats, scheduled tasks and publish Expo apps to the mobile app.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/opencuria/OpenCuria/main/install.sh | sudo bash
```

The installer asks what the host should run:

- **Everything**: the control plane in Docker Compose (backend, web app, PostgreSQL,
  Redis, Caddy with automatic HTTPS) plus a runner as a systemd service, registered
  with the control plane.
- **Control plane only**: add runners later under **Settings → Runners → Register
  Runner**; the dialog shows the install command for the runner host.
- **Runner only**: asks for the control plane URL and a runner token.

Runners need an x86_64 host with KVM running Debian 12+ or Ubuntu 22.04+. For HTTPS,
point a domain at the server and open ports 80 and 443; without a domain the web
app is served over plain HTTP on the server IP.

On a host that already runs OpenCuria, the same command offers to **update** what is
installed (configuration and data are kept) or to **uninstall** OpenCuria completely,
including all workspaces, the database and its files; Docker, QEMU and libvirt stay
installed. Unattended: `… | sudo bash -s -- runner --url URL --token TOKEN`,
`… -s -- update` or `… -s -- uninstall --yes` (`--help` lists all options).

The control plane lives in `/opt/opencuria` (`compose.yml`, `.env`; back up the
`.env`, it holds the database password and encryption key), the runner in
`/opt/opencuria/runner` (settings in its `.env`, see [runner/README.md](runner/README.md);
Windows 11 images need a Windows 11 ISO in `RUNNER_WINDOWS_ISO`).

To use your own reverse proxy instead of the bundled Caddy, remove
`COMPOSE_PROFILES=proxy` from `.env`, publish the ports (`WEBAPP_PORT=8080`,
`BACKEND_PORT=8000`) and route `/api` and `/ws` to the backend (with WebSocket
upgrades, unbuffered) and everything else to the web app on the same origin. Workspace
files stream through `/api/v1/workspace-files/`: allow request bodies of 500 MB there
(nginx: `client_max_body_size 500m;`, `proxy_request_buffering off;`).

## First workspace

No image is built during installation. Once the runner is online, an admin builds
an image definition on it under **Settings → Image Definitions**: the bundled
`Ubuntu 24.04`, `Windows 11` or a custom one. Workspaces can be created once the
build is ready. Windows workspaces follow the license model chosen under
**Settings → Windows** (product keys, per-user licenses with KMS, or explicitly
without license).

## Develop from source

```bash
./scripts/setup-dev.sh      # system packages, Python venvs, npm ci, migrations
npm run dev:web             # or: npm run dev:mobile
```

Backend (each in its own terminal, from `backend/`; migrate after every update):

```bash
.venv/bin/python manage.py migrate --noinput
.venv/bin/uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --workers 1 \
  --lifespan on --ws websockets-sansio --ws-max-size 209715200
.venv/bin/python manage.py execute_harness
.venv/bin/python manage.py recover_lifecycle
```

For a local runner, point `RUNNER_BACKEND_URL` at this backend and run
`python -m src` in `runner/`. SQLite is for development; use PostgreSQL in production.

## Operations

- **Processes:** one API gateway (terminal and desktop sessions hold gateway-local
  state), one `execute_harness` worker (agent runs, scheduler, notifications,
  recovery) and one `recover_lifecycle` worker. Redis connects them. API restarts
  leave runs going; without a live execution worker, agent requests fail with 503.
- **Systemd:** `sudo ./backend/systemd/install-recovery-service.sh` installs both
  workers for a source checkout (`--help` for options).
- **Stuck runner operations:** use **Check status** in the runner's activity list;
  reconcile, retry and acknowledge are under `/api/v1/runners/operations/{id}/`.
- **Shared storage:** API and execution worker must share `MEDIA_ROOT`. Multi-host
  PostgreSQL deployments must set `EXECUTION_PROCESS_LOCK_PATH` on shared storage.
- **Upgrades:** deploy matching backend and runner versions (the installer does);
  the backend migrates on start. Keep the runner's `RUNNER_STATE_DIR` with its disks
  and snapshots; never share or delete it.
- **Push notifications:** put `apns.p8` and/or `firebase.json` into
  `/opt/opencuria/secrets/push/` (mounted at `/run/secrets/`) and set `APNS_TEAM_ID`,
  `APNS_KEY_ID`, `APNS_PRIVATE_KEY_FILE=/run/secrets/apns.p8` and/or
  `FCM_SERVICE_ACCOUNT_FILE=/run/secrets/firebase.json` in `.env`.
- **MCP OAuth** needs an `https://` `PUBLIC_URL`.

CI publishes multi-arch images `ghcr.io/opencuria/backend` and
`ghcr.io/opencuria/webapp` from `main`.

## License

GNU Affero General Public License v3.0, see [`LICENSE`](./LICENSE). Third-party
components: [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md).
