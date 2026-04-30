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

After that you can run `backend`, `webapp`, and `runner` individually.

For a local Linux setup with backend/webapp from source and a native QEMU
runner on the same machine, use the same QEMU runner flow above, but point
`RUNNER_BACKEND_URL` at your local backend and run the runner in the foreground
with `python -m src serve` while iterating.

## License

This project is licensed under the GNU Affero General Public License v3.0.
See [`LICENSE`](./LICENSE).
