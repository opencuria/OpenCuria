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

### 3. Remote QEMU runner

If a runner host should support QEMU/KVM, run it natively on Linux instead of inside a container.

Use:

- [`runner/systemd/opencuria-runner.service`](./runner/systemd/opencuria-runner.service)
- [`runner/.env.example`](./runner/.env.example)
- [`runner/scripts/setup-native-qemu-runner.sh`](./runner/scripts/setup-native-qemu-runner.sh)

Why native for QEMU:

- better libvirt/KVM integration
- simpler disk and snapshot permissions
- less operational overhead than containerized libvirt

Recommended host requirements:

- Ubuntu 24.04 or another modern Debian/Ubuntu-based Linux host
- KVM available (`/dev/kvm`)
- root or sudo access
- network reachability from runner host to the OpenCuria backend

Example end-to-end setup on the runner host:

```bash
git clone https://github.com/opencuria/OpenCuria.git
cd OpenCuria
sudo ./runner/scripts/setup-native-qemu-runner.sh
sudoedit /etc/opencuria/runner.env
sudo systemctl start opencuria-runner.service
sudo systemctl status opencuria-runner.service
```

The setup script is idempotent and handles:

- apt package installation for QEMU/KVM, libvirt, Python build dependencies and ISO tooling
- `/opt/opencuria/runner` installation via `rsync`
- `.venv` creation plus `pip install -r requirements.txt`
- creation of `/var/lib/opencuria/{images,disks,snapshots,base-images}`
- installation of `/etc/opencuria/runner.env` without overwriting an existing file
- installation and enablement of the `systemd` unit

What to set in `/etc/opencuria/runner.env` before starting:

- `RUNNER_API_TOKEN=<runner token from backend>`
- `RUNNER_BACKEND_URL=http://<backend-host>:8000`
- `RUNNER_ENABLED_RUNTIMES=qemu`

Useful validation commands:

```bash
ls -l /dev/kvm
virsh -c qemu:///system list --all
systemctl status libvirtd
journalctl -u opencuria-runner.service -f
```

If you update the checkout later, rerun the same script:

```bash
cd OpenCuria
git pull
sudo ./runner/scripts/setup-native-qemu-runner.sh --start
```

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

## 🔁 Migration Notes

The old deployment files were replaced:

- `docker-compose.yml` → [`compose.yml`](./compose.yml)
- root production compose → [`compose.server.yml`](./compose.server.yml)
- `runner/docker-compose.yml` → [`runner/compose.yml`](./runner/compose.yml)

## License

This project is licensed under the GNU Affero General Public License v3.0.
See [`LICENSE`](./LICENSE).
