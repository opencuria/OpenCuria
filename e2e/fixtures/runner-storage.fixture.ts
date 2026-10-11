import type { Page } from "@playwright/test";
import type {
  RunnerStorage,
  StorageResource,
} from "../../webapp/src/types/runnerStorage";

const GiB = 1024 ** 3;

/** Only HTTP and Socket.IO are fixtures; the Vue router, stores and components are real. */
export async function installRunnerBackend(
  page: Page,
  overrides?: { snapshot?: RunnerStorage; runnerName?: string },
) {
  const errors: string[] = [],
    unexpected: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const now = new Date(Date.now() - 1000).toISOString();
  const workspaces = ["Atlas", "Beacon", "Cedar", "Delta"].map(
    (name, index) => ({
      id: `ws-${index}`,
      name,
      owner_id: "1",
      owner_label: "Storage Tester",
      status: index === 0 ? "running" : "stopped",
      observed_state: index === 0 ? "running" : "stopped",
      base_image_instance_id: index < 2 ? "image-a" : "image-b",
      last_activity_at: now,
    }),
  );
  function resource(
    id: string,
    kind: string,
    bytes: number | null,
    extra: Partial<StorageResource> = {},
  ): StorageResource {
    return {
      physical_id: id,
      kind,
      managed: true,
      state: "present",
      filesystem_id: "fs-qemu-root",
      file_identity: `inode:${id}`,
      allocated_bytes: bytes,
      logical_bytes: bytes,
      virtual_bytes: kind === "disk" ? 40 * GiB : null,
      shared_bytes: null,
      reclaimable_bytes: null,
      aliases: [],
      dependencies: [],
      image_id: null,
      workspace: null,
      provenance: "runner inventory",
      ...extra,
    };
  }
  const snapshot: RunnerStorage = {
    runner_id: "storage-runner",
    runner_online: true,
    latest_snapshot_id: 1,
    latest_complete: true,
    runtimes: [
      {
        runtime_type: "qemu",
        fresh: true,
        snapshot_id: 1,
        collected_at: now,
        received_at: now,
        // Same filesystem at two paths must not double the chart capacity.
        filesystems: ["/var/lib/qemu", "/var/lib/qemu/images"].map((path) => ({
          filesystem_id: "fs-qemu-root",
          path,
          capacity_bytes: 100 * GiB,
          used_bytes: 70 * GiB,
          available_bytes: 25 * GiB,
        })),
        resources: [
          resource("/images/ubuntu.qcow2", "image", 10 * GiB, {
            image_id: "image-a",
          }),
          resource("/images/research.qcow2", "image", 8 * GiB, {
            image_id: "image-b",
          }),
          ...workspaces.flatMap((workspace, index) => [
            resource(`vm:${workspace.id}`, "workspace", 0, {
              workspace,
              state: workspace.status,
              dependencies: [
                `/workspaces/${workspace.id}/disk.qcow2`,
                `/workspaces/${workspace.id}/seed.iso`,
              ],
            }),
            resource(
              `/workspaces/${workspace.id}/disk.qcow2`,
              "disk",
              (index + 2) * GiB,
              {
                workspace,
                dependencies: [
                  index < 2 ? "/images/ubuntu.qcow2" : "/images/research.qcow2",
                ],
              },
            ),
            resource(`/workspaces/${workspace.id}/seed.iso`, "file", GiB / 4, {
              workspace,
            }),
          ]),
          resource("/orphan/unmeasured.qcow2", "disk", null, {
            managed: false,
          }),
        ],
        diagnostics: {
          complete: true,
          errors: [],
          foreign_resource_count: 1,
          collected_at: now,
        },
      },
    ],
    generations: ["Ubuntu base", "Research image"].map((name, index) => ({
      id: index === 0 ? "image-a" : "image-b",
      name,
      owner_label: "Storage Tester",
      runtime_type: "qemu",
      definition_id: `definition-${index}`,
      definition_name: name,
      build_job_id: `build-${index}`,
      generation: 1,
      status: "ready",
      assignment_status: "ready",
      runner_ref:
        index === 0 ? "/images/ubuntu.qcow2" : "/images/research.qcow2",
      size_bytes: 90 * GiB,
      size_source: "logical",
      origin_type: "definition_build",
      revision_id: null,
      is_legacy: false,
      is_current: true,
      is_pending: false,
      observed_state: "present",
      dependencies: workspaces.filter(
        (ws) =>
          ws.base_image_instance_id === (index === 0 ? "image-a" : "image-b"),
      ),
    })),
    operations: [],
    capture_requests: [],
    activities: [],
  };
  if (overrides?.snapshot)
    Object.assign(snapshot, overrides.snapshot, {
      runner_id: "storage-runner",
    });
  const runner = {
    id: "storage-runner",
    name: overrides?.runnerName || "Runner A",
    status: "online",
    available_runtimes: ["qemu", "docker"],
    connected_at: now,
    disconnected_at: null,
    qemu_max_vcpus: 16,
    qemu_max_memory_mb: 32768,
    qemu_max_disk_gb: 400,
  };
  // Host telemetry intentionally differs from the storage filesystem and guest disk capacity.
  const telemetry = {
    runner_id: runner.id,
    timestamp: now,
    cpu_usage_percent: 23,
    ram_used_bytes: 12 * GiB,
    ram_total_bytes: 32 * GiB,
    disk_used_bytes: 150 * GiB,
    disk_total_bytes: 500 * GiB,
    vm_metrics: {
      "ws-0": {
        cpu_usage_percent: 17,
        ram_used_bytes: GiB,
        ram_total_bytes: 2 * GiB,
        disk_used_bytes: GiB,
        disk_total_bytes: 40 * GiB,
      },
    },
  };
  const token = Buffer.from(
    JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }),
  ).toString("base64url");
  await page.addInitScript((token) => {
    localStorage.setItem("kern_access_token", token);
    localStorage.setItem("kern_refresh_token", "storage-refresh");
    localStorage.setItem("kern_active_org_id", "storage-org");
  }, `fixture.${token}.signature`);
  await page.routeWebSocket(/\/ws\/runner/, (socket) => {
    socket.send(
      "0" +
        JSON.stringify({
          sid: "storage-socket",
          upgrades: [],
          pingInterval: 100000,
          pingTimeout: 100000,
        }),
    );
    socket.onMessage((message) => {
      if (String(message).startsWith("40/frontend"))
        socket.send(
          "40/frontend," + JSON.stringify({ sid: "storage-frontend" }),
        );
      else if (message === "2") socket.send("3");
    });
  });
  let refreshes = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const method = route.request().method();
    const json = (data: unknown) => route.fulfill({ json: data });
    if (method === "GET") {
      if (path === "/auth/me/")
        return json({
          id: 1,
          email: "storage@example.test",
          first_name: "Storage",
          last_name: "Tester",
          organizations: [
            {
              id: "storage-org",
              name: "Storage fixture",
              slug: "storage",
              role: "admin",
            },
          ],
        });
      if (path === "/runners/") return json([runner]);
      if (path === "/runners/storage-runner/storage/") return json(snapshot);
      if (path === "/runners/storage-runner/metrics/latest/")
        return json(telemetry);
      if (path === "/runners/storage-runner/metrics/history/")
        return json([telemetry]);
      if (path === "/provider-config/") return json({});
      if (
        [
          "/workspaces/",
          "/harness/conversations/",
          "/scheduled-tasks/",
          "/image-definitions/",
          "/skills/",
          "/provider-config/models/",
          "/recent-models/",
          "/agent-configs/",
          "/provider-config/providers/",
          "/image-artifacts/",
          "/image-artifacts/deletions/",
        ].includes(path)
      )
        return json([]);
    }
    if (
      method === "POST" &&
      path === "/runners/storage-runner/storage/refresh/"
    ) {
      refreshes++;
      const requested_at = new Date(Date.now() - 2000).toISOString();
      const received = new Date(Date.now() - 1000).toISOString();
      snapshot.latest_snapshot_id = 2;
      snapshot.runtimes[0]!.snapshot_id = 2;
      snapshot.runtimes[0]!.collected_at = received;
      snapshot.runtimes[0]!.received_at = received;
      snapshot.runtimes[0]!.resources.find(
        (r) => r.physical_id === "/workspaces/ws-0/disk.qcow2",
      )!.allocated_bytes = 3 * GiB;
      return json({ requested_at });
    }
    unexpected.push(`${method} ${path}`);
    return route.fulfill({
      status: 501,
      json: { detail: `Unexpected storage fixture request: ${method} ${path}` },
    });
  });
  return { errors, unexpected, snapshot, refreshes: () => refreshes };
}
