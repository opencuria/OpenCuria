# Runner

Executes QEMU/KVM workspaces and exposes lifecycle, exec, files, terminal, desktop
and Git over Socket.IO. It has no agent, plugin or capacity policy; the backend
decides and the runner executes. Installation: [distributed setup](../README.md#qemu-runner).

## Requirements

- Linux with KVM, QEMU 5.1+ and libvirt 6.9+ (free page reporting returns unused
  guest RAM to the host).
- Image definitions use an `ubuntu:<version>` base; the cloud image is downloaded
  into `RUNNER_QEMU_IMAGE_CACHE_DIR` on first use.
- Custom workspace images need Git 2.31+.

## Configuration

Settings come from `runner/.env` (see `.env.example`); all options and defaults are
in `src/config.py`.

| Setting | Purpose |
| --- | --- |
| `RUNNER_API_TOKEN`, `RUNNER_BACKEND_URL` | Backend connection |
| `RUNNER_STATE_DIR` | Lifecycle journal; the systemd unit uses `/var/lib/opencuria-runner` |
| `RUNNER_QEMU_IMAGE_CACHE_DIR`, `_DISK_DIR`, `_SNAPSHOT_DIR` | Storage; built images go to `/var/lib/opencuria/base-images` |
| `RUNNER_QEMU_VCPUS`, `_MEMORY_MB`, `_DISK_SIZE_GB` | Default workspace resources |
| `RUNNER_QEMU_BUILD_*` | Image build guests: resources, concurrency, queue and script timeouts |
| `RUNNER_TERMINAL_*` | Terminal lease, idle timeout and per-workspace limit |

## Operations

- Restart the runner only when no agent run is active. Lost SSH streams end their
  commands; issued commands and agent runs are never replayed.
- Keep `RUNNER_STATE_DIR` together with disks and snapshots across upgrades. Never
  share it between runners or delete it to resolve a failed operation.
- Images count as ready only with a verified `.manifest.json`; cloning checks size
  and SHA-256 first.

## Code map

- `src/operation_executor.py`: admission, recovery and replay of lifecycle commands;
  `src/journal.py` only stores records.
- `src/runtime/`: QEMU runtime, pinned runtime artifacts (`artifact_manifest.json`:
  Codex app-server, Computer Use from [`packages/computer-use`](../packages/computer-use/))
  and `diagnostics.py`. Failures publish a stable `diagnostic_code`, never exception text.
- `src/interfaces/`: Socket.IO transport.

The import graph must stay acyclic (`tests/test_module_dependencies.py`).
