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

## VM memory reclamation

VMs enable virtio-balloon [free page reporting][free-page-reporting] so the host
can reclaim genuinely free guest RAM after a workload releases it. The configured
guest RAM remains available; running applications and the guest filesystem cache
are not evicted. This requires libvirt 6.9+, QEMU 5.1+, and a Linux guest with
virtio-balloon page reporting (Linux 5.7+, including the Ubuntu 22.04/24.04 images).

Keep the [Linux reporting defaults][linux-page-reporting]: on these guest kernels,
reports are batched with a two-second delay and use large free blocks (typically
2 MiB on x86_64). This limits reclaim/reallocation churn and preserves useful
cache without a runner polling loop or forced balloon inflation. Reusing reclaimed
pages still requires host page faults. Repeated large allocations separated by
pauses longer than the reporting delay can incur measurable overhead; zero
overhead cannot be guaranteed for all workloads. Memory retained in an
application's allocator or in useful guest cache is not free guest memory and
will not immediately reduce QEMU's host RSS.

After updating, restart the runner when no agent run is active. New workspaces,
clones, and image-build VMs enable reporting automatically. Existing workspaces
are upgraded on their next stop/start or resource change;
running VMs are not interrupted. No image rebuild or seed regeneration is needed.
Verify host reclamation using the QEMU process's RSS after allocating, touching,
and freeing guest memory; `virsh dommemstat` free-memory counters do not measure
the host pages returned by this feature.

[free-page-reporting]: https://libvirt.org/formatdomain.html#memory-balloon-device
[linux-page-reporting]: https://docs.kernel.org/mm/free_page_reporting.html

## Workspace Git

Git integration requires Git 2.31+ inside the workspace (`--porcelain=v2` and
`rev-parse --path-format=absolute`). The Ubuntu 22.04/24.04 images already
provide this; custom images must provide a current Git as well.

## QEMU image bootstrap

QEMU images are built from image definitions and build jobs in the app. The
backend generates the init script; the runner builds the QCOW2 image from an
Ubuntu cloud image.

Cloud images are cached in `RUNNER_QEMU_IMAGE_CACHE_DIR`. Definition builds are
published to `/var/lib/opencuria/base-images`, the path used by the control
plane. Both publication directories (builds and snapshots) are validated and
included in storage inventory; no cache-directory override is needed.
Runner startup initializes the configured disk, snapshot and cloud-image cache
directories before the first inventory; missing directory permissions fail
startup with the affected path and setup command.

Image builds provision separate VMs concurrently. Temporary build guests default
to 1 vCPU and 2048 MiB, with at most two active builds per runner. Configure
`RUNNER_QEMU_BUILD_VCPUS`, `RUNNER_QEMU_BUILD_MEMORY_MB` and
`RUNNER_QEMU_BUILD_CONCURRENCY` to match the host's available resources and build
workloads; these positive values are independent of workspace resource defaults.
The effective build concurrency also respects the host's logical CPU count.
Builds wait for a slot or temporary host RAM pressure to clear, reporting the
queue reason without allocating a guest. `RUNNER_QEMU_BUILD_QUEUE_TIMEOUT`
(default 1800 seconds) bounds admission waiting. Requests exceeding the host's
physical CPU/RAM budget fail immediately with a configuration diagnostic.
Every guest activation (create, clone, build, start and resource-change restart)
checks available host RAM and the configured RAM of every active libvirt guest.
Workspace activations that would consume the host margin (at least 1 GiB or 10%
of host RAM) fail before starting a guest and can be retried after memory becomes
available; image builds wait automatically within their queue deadline.
Creation and cloning check admission before allocating disks or networks; paused
guests retain their existing reservation when resumed.
Create, clone and resource changes reject nonpositive CPU/RAM/disk settings and
vCPU requests exceeding host capacity; explicit zero never selects a default.
Queued builds do not allocate guests. Guest disk overlays remain sparse and
published images are flattened, verified and durably committed before readiness.
Workspace readiness and graceful shutdown hold only the affected guest's lifecycle
lock, allowing other guests and inventory scans to continue.
Direct status, discovered workspaces and inventory share the same libvirt state
mapping. Paused, suspended, shutting-down and crashed guests never appear usable
merely because their domains remain active. Nonstopped observations also revoke
older stopped credential-scrub proof.

Subnet allocation, publication and removal still serialize against storage scans, while guest provisioning and
SSH readiness do not hold the shared storage lock. libvirt assigns unique bridge
names, including when older interrupted build VMs still exist.
Temporary build guests are named from the durable operation UUID. Recovery can
remove the exact interrupted guest without guessing ownership from image paths.
Interrupted workspace allocation before domain definition can also settle
automatically once complete inventory proves its partial files have no consumers.
Files remain preserved with unknown credential state. A later ordinary remove
reconstructs a missing runtime identity only from an exact durable create/clone
admission and a fresh ownership/quiescence check.
Completed removal revokes that allocation lineage; a later same-ID create
establishes a new one. Arbitrary external replacement of owned guest resources
is outside this ownership model.

New guests use their network gateway's [libvirt DNS resolver][libvirt-dns], which
forwards via the host's resolver configuration. This also works with host-local
resolvers such as systemd-resolved; no public DNS fallback is installed. Existing
guests retain their seed configuration until reset or recreation.

Desktop transport uses the authenticated WebSocket proxy. The runner passes
[`-PublicIP 127.0.0.1`][kasm-public-ip] to Xvnc even for existing images, disabling
automatic public IP discovery. The proxied viewer disables WebRTC before the client loads
so it does not contact public STUN servers. Restart the desktop after updating.

[libvirt-dns]: https://libvirt.org/formatnetwork.html#addressing
[kasm-public-ip]: https://kasmweb.com/kasmvnc/docs/master/man/Xvnc.html

Built and captured images use `.manifest.json` publication markers. Inventory
only classifies images with verified manifests as ready; cloning verifies the
manifest, byte count and SHA-256 before allocating a guest. Unpublished disks
remain visible with their physical dependencies
and cannot be deleted as image artifacts. Publication and inventory include the
measured virtual disk size and minimum GiB, so provisioning can reserve the same
capacity the guest receives. Requests below a source image's actual minimum fail
before creating its disk or network.
Invalid publication markers leave readable images unknown and unusable, while
their inspected backing dependencies still protect referenced storage. They do
not block unrelated workspace cleanup. If the disk header or backing graph
cannot be read, mutation remains blocked until storage can be inspected again.

Initial workspace creation, cloning and image builds wait for cloud-init to
finish before reporting readiness or running the image script. The read-only
status probe tolerates the first-boot SSH restart; the image script is never
replayed. `RUNNER_QEMU_CLOUD_INIT_TIMEOUT` bounds this wait (default 300 seconds).

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

Stopping, removing or reconfiguring a workspace invalidates connection ownership
before cleanup; late connections cannot repopulate the cache. Small per-instance
generation/lock tombstones remain stable for queued callers and same-ID recreation. Genuine
transport loss still ends existing streams: new calls reconnect, but issued
commands and agent runs are **never automatically replayed**. A missing remote
exit status is unknown, not exit 0; integer-only exec/build paths fail explicitly.

Requires AsyncSSH 2.15+ (the public connection-closed API). After updating runner
code/dependencies, restart the runner when no agent run is active; restarting
cannot migrate existing SSH streams. No guest/image changes are required.

## Pinned external runtimes

`ensure_runtime_artifact("codex-agent", "0.166.0")` provisions the official
`rust-v0.166.0` **standalone app-server package**, not the single-binary release.
The returned executable is
`/opt/opencuria/runtimes/codex-agent/0.166.0/bin/codex-app-server`; the package
also retains `bin/codex-code-mode-host`, `bin/codex-exec-server`,
`codex-resources/bwrap`, and `codex-path/rg`. Official x86_64 and aarch64 musl package SHA-256/size pins are
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

## Computer Use

The product-owned Computer Use stdio runtime is installed through
`ensure_runtime_artifact("computer-use", "0.1.0")`. See the
[Computer Use guide](../docs/computer-use.md) for architecture, dependencies,
rollout, immutable bundle builds and real desktop verification.
