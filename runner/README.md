# Runner

The runner executes QEMU/KVM workspaces and exposes lifecycle, execution,
files, terminal, desktop, and git interfaces. Agent and plugin policy belongs to
the backend. See the root [distributed setup](../README.md#-distributed-setup)
for native Linux installation, or [source setup](../README.md)
for local development.

## Operations

- [Managed desktop resources](../docs/managed-desktop-resources.md): generic
  leases, persistent SQLite ownership stores, guest control directories,
  process-group cleanup limits, epoch recovery, and coordinated rollout.
- [Lifecycle recovery](../docs/image-lifecycle-implementation.md#recovery-hardening-operator-workflow).

Desktop startup and cleanup use the runner-owned `desktop_stop.sh`, including
for existing workspace images. Process matching is anchored to desktop
executables and display `:1`; arbitrary argv text such as the agent wrapper's
`profile` must never match `rofi`. Image helper copies are checked against this
canonical script. Restart the runner after updating; no image rebuild is needed.

Workspace removal stops active VMs, including paused guests, and verifies that
their libvirt domain is gone before removing the network, disk, seed ISO, or host
key. Failed stop, undefine, or domain enumeration retains those resources for
retry. Incomplete storage inventories and physical dependents still block removal.

## Workspace Git

Git integration requires Git 2.31+ inside the workspace (`--porcelain=v2` and
`rev-parse --path-format=absolute`). The Ubuntu 22.04/24.04 images already
provide this; custom images must provide a current Git as well.

## QEMU image bootstrap

QEMU images are built from image definitions and build jobs in the app. The
backend generates the init script; the runner builds the QCOW2 image from an
Ubuntu cloud image.

Built and captured images use `.manifest.json` publication markers. Inventory
only classifies images with verified manifests as ready; cloning requires a
manifest. Unpublished disks remain visible with their physical dependencies
and cannot be deleted as image artifacts.

Each newly provisioned VM gets a fresh
[cloud-init instance ID](https://docs.cloud-init.io/en/26.1/reference/datasources/nocloud.html#example-creating-a-disk)
in its seed ISO, independent of the stable workspace ID. This makes reset, update, and cloning
apply the new network and SSH configuration even when the image contains cached
cloud-init state from the same workspace. Start, stop, and restart reuse that ISO;
they do not generate another bootstrap identity.

## QEMU SSH resilience

Open process/terminal channels share a cached SSH transport. Acquiring it never
executes a liveness command: a slow guest or a refused session must not disconnect
unrelated agent streams. Protocol keepalives run after 30 seconds of inactivity
and tolerate five unanswered requests (about three minutes without incoming
traffic before failure detection; not a hard deadline under continuing traffic).

Guest health checks use one disposable, separately authenticated SSH connection
per instance, with the same pinned host-key policy. Their 15-second deadline and
bounded transport cleanup also cover a stalled session open without leaking
channels or closing the agent transport. Health observations never restart a VM.

Explicit stop/restart/remove invalidates connection ownership before cleanup;
late connections cannot repopulate the cache. Small per-instance generation/lock
tombstones remain stable for queued callers and same-ID recreation. Genuine
transport loss still ends existing streams: new calls reconnect, but issued
commands and agent runs are **never automatically replayed**. A missing remote
exit status is unknown, not exit 0; integer-only exec/build paths fail explicitly.

Requires AsyncSSH 2.15+ (the public connection-closed API). After updating runner
code/dependencies, restart the runner when no agent run is active; restarting
cannot migrate existing SSH streams. No guest/image changes are required.

## Pinned external runtimes

`ensure_runtime_artifact("codex-agent", "0.163.0")` provisions the official
`rust-v0.163.0` **standalone app-server package**, not the single-binary release.
The returned executable is
`/opt/opencuria/runtimes/codex-agent/0.163.0/bin/codex-app-server`; the package
also retains `bin/codex-code-mode-host`, `codex-resources/bwrap`, and
`codex-path/rg`. Official x86_64 and aarch64 musl package SHA-256/size pins are
in `src/runtime/codex_artifact.py` and `artifact_manifest.json`; the static musl
packages serve both glibc and musl workspaces. Provisioning requires Linux
Python 3.10+ and libc `renameat2` for atomic no-replace directory publication.

The shared artifact manager retains cache locks, download limits, cancellation
settlement and workspace fences. Codex extraction allows only the pinned
package layout, caps decompressed bytes, and generates per-file hashes from the
verified archive. The guest verifies every installed file before executing the
version probe and again before returning. Existing altered artifacts fail;
they are not repaired or replaced. The combined manifest was deliberately
repinned; Claude release signature and platform pins remain unchanged.

Managed processes with an `agent` owner select the Codex isolated environment
by an explicit `CODEX_HOME` under
`/workspace/.opencuria/harness/codex/<session-uuid>/config` (optionally followed
by `/resume-<16 lowercase hex characters>`). All config components must already
exist and must not be symlinks. Only `CODEX_HOME` and an optional
`OPENCURIA_CODEX_API_KEY` are accepted. The runner supplies a disposable HOME
and a fixed PATH including the packaged ripgrep. Ambient credentials and
caller-supplied HOME/PATH are excluded. Explicit environment values travel only
in a length-framed private stdin preamble, never process argv or durable records.
Subscription authentication can subsequently use app-server stdin, without an
authentication environment variable. Model, tool, approval and prompt policy
remain outside the runner.
