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

## Quick start (local)

```bash
cp .env.example .env
docker compose up -d
```

- Web app: `http://127.0.0.1:8080` (login: `LOCAL_ADMIN_EMAIL` / `LOCAL_ADMIN_PASSWORD`)
- API docs: `http://127.0.0.1:8000/api/v1/docs`

Compose bootstraps the admin, organization and runner record. Then set up a native
runner as below, using `LOCAL_RUNNER_API_TOKEN` as its token.

## Distributed setup

### Central server

```bash
cp .env.server.example .env
docker compose -f compose.server.yml up -d
```

This starts PostgreSQL, Redis, the backend (API, execution and lifecycle recovery
workers) and the webapp. A reverse proxy must route `/api` and `/ws` to the backend
(with WebSocket upgrades, unbuffered) and everything else to the webapp.

### QEMU runner

The runner needs a Linux host with KVM. Create a runner API token in the backend
first, then on the runner host:

```bash
sudo apt-get install -y qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients \
  genisoimage build-essential python3-dev pkg-config libvirt-dev
cd runner
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set RUNNER_API_TOKEN and RUNNER_BACKEND_URL
sudo install -d -m 755 /var/lib/opencuria/{images,base-images,disks,snapshots}
cd .. && sudo ./runner/systemd/install-runner-service.sh
sudo systemctl enable --now opencuria-runner
sudo journalctl -u opencuria-runner -f
```

The runner should then show up online in the backend.

## First workspace

No image is built during installation. Once the runner is online, an admin builds
an image definition on it under **Settings → Image Definitions**: the bundled
`Ubuntu 24.04` or a custom one. Workspaces can be created once the build is ready.

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
- **Upgrades:** run migrations first and deploy matching backend and runner versions.
  Keep the runner's `RUNNER_STATE_DIR` with its disks and snapshots; never share or
  delete it.
- **Push notifications:** set `APNS_TEAM_ID`, `APNS_KEY_ID`, `APNS_PRIVATE_KEY_FILE`
  and/or `FCM_SERVICE_ACCOUNT_FILE`; Compose mounts `./secrets/push/` at `/run/secrets/`.
- **MCP OAuth:** set `MCP_OAUTH_CALLBACK_URL` (public HTTPS) and
  `MCP_OAUTH_FRONTEND_RETURN_URL` on the same hostname as the frontend.

CI publishes multi-arch images `ghcr.io/opencuria/backend` and
`ghcr.io/opencuria/webapp` from `main`.

## License

GNU Affero General Public License v3.0, see [`LICENSE`](./LICENSE). Third-party
components: [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md).
