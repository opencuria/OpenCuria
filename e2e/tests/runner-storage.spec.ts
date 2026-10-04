import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import type {
  RunnerStorage,
  StorageResource,
} from "../../webapp/src/types/runnerStorage";

const GiB = 1024 ** 3;
const artifacts = resolve(__dirname, "../../..", ".opencuria/playwright");

/** Only HTTP and Socket.IO are fixtures; the Vue router, stores and components are real. */
async function installBackend(page: Page) {
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
  };
  const runner = {
    id: "storage-runner",
    name: "Runner A",
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
  return { errors, unexpected, refreshes: () => refreshes };
}

test("real runner storage chart, merged map, inspection and refresh", async ({
  page,
}, testInfo) => {
  const backend = await installBackend(page);
  await page.goto("/?settings=runners");
  await page.getByRole("button", { name: "Runner A", exact: true }).click();
  const cockpit = page.getByTestId("runner-cockpit");
  const chart = cockpit.getByRole("group", { name: "Two-ring storage chart" });
  const map = cockpit.getByRole("region", {
    name: "Runner dependencies",
    exact: true,
  });
  await expect(chart).toBeVisible();
  await expect(
    cockpit.getByRole("heading", { name: "Runner A", exact: true }),
  ).toBeVisible();
  await expect(
    map.getByRole("button", { name: "Inspect Atlas", exact: true }),
  ).toBeVisible();
  await expect(map.locator(".topology-columns > section")).toHaveCount(2);
  await expect(
    map.getByRole("button", { name: /Workspace disk|Boot seed/ }),
  ).toHaveCount(0);
  await expect(chart.locator('[data-segment-key^="inner-"]')).toHaveCount(4);
  await expect(chart.locator('[data-segment-key^="outer-"]')).toHaveCount(8);
  // Actual blocks: A = 10 + 2.25 + 3.25; B = 8 + 4.25 + 5.25; Other = 42; Free = 25.
  for (const label of [
    "Ubuntu base · Image + workspaces: 15.5 GiB · 15.5%",
    "Research image · Image + workspaces: 17.5 GiB · 17.5%",
    "Other: 42.0 GiB · 42.0%",
    "Free: 25.0 GiB · 25.0%",
  ])
    await expect(
      chart.getByRole("img", { name: label, exact: true }).first(),
    ).toBeAttached();
  await expect(chart).toContainText("100.0 GiB");
  await expect(
    cockpit.getByRole("region", { name: "Resource overview" }),
  ).toContainText("500.0 GiB");
  await expect(cockpit.getByTestId("runner-storage-donut")).toContainText(
    "1 file allocation unmeasured",
  );

  const atlas = chart.getByRole("button", {
    name: "Atlas: 2.3 GiB · 2.3%",
    exact: true,
  });
  // SVG path bounding-box centers may be outside an annular wedge; click its actual painted edge.
  await atlas.scrollIntoViewIfNeeded();
  const point = await atlas.evaluate((element: SVGPathElement) => {
    const path = element;
    const svg = path.ownerSVGElement!;
    const matrix = svg.getScreenCTM()!;
    const first = path.getPointAtLength(12);
    const p = new DOMPoint(first.x, first.y).matrixTransform(matrix);
    const rect = path.getBoundingClientRect();
    return { x: p.x - rect.left, y: p.y - rect.top };
  });
  await atlas.click({ position: point });
  const inspector = cockpit.getByTestId("runner-inspector");
  await expect(
    map.getByRole("button", { name: "Inspect Atlas", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    inspector.getByRole("heading", { name: "Atlas", exact: true }),
  ).toBeVisible();
  await expect(inspector).toContainText(/Observed:\s*running/i);
  await expect(
    inspector
      .locator("dl > div")
      .filter({ has: page.getByText("Used storage", { exact: true }) }),
  ).toContainText("2.3 GiB");

  await cockpit
    .getByRole("button", { name: "Refresh inventory", exact: true })
    .click();
  await expect.poll(backend.refreshes).toBe(1);
  await expect(
    inspector
      .locator("dl > div")
      .filter({ has: page.getByText("Used storage", { exact: true }) }),
  ).toContainText("3.3 GiB");
  await expect(
    map.getByRole("button", { name: "Inspect Atlas", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    cockpit.getByRole("button", { name: "Refresh inventory", exact: true }),
  ).toBeEnabled();

  await map
    .getByRole("textbox", { name: "Search dependency map" })
    .fill("/workspaces/ws-0/seed.iso");
  await expect(
    map.getByRole("status").filter({ hasText: "matches" }),
  ).toContainText("1 matches");
  await expect(
    map.getByRole("button", { name: "Inspect Atlas", exact: true }),
  ).toBeVisible();
  await expect(map.getByRole("button", { name: /Boot seed/ })).toHaveCount(0);
  await map.getByRole("textbox", { name: "Search dependency map" }).fill("");
  const image = chart.getByRole("button", {
    name: "Ubuntu base: 10.0 GiB · 10.0%",
    exact: true,
  });
  await image.focus();
  await expect(image).toBeFocused();
  await image.press("Enter");
  await expect(
    map.getByRole("button", { name: "Inspect Ubuntu base", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    inspector.getByRole("heading", { name: "Ubuntu base", exact: true }),
  ).toBeVisible();

  await cockpit.getByRole("tab", { name: "Inventory", exact: true }).click();
  await cockpit
    .getByRole("button", { name: "Physical resources · 15", exact: false })
    .click();
  await expect(
    cockpit.getByRole("button", {
      name: "Inspect resource Atlas · Workspace disk",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    cockpit.getByRole("button", {
      name: "Inspect resource Atlas · Boot seed",
      exact: true,
    }),
  ).toBeVisible();
  await cockpit.getByRole("tab", { name: "Overview", exact: true }).click();
  await cockpit.getByRole("combobox", { name: "Filter runtime" }).click();
  await page.getByRole("option", { name: "DOCKER", exact: true }).click();
  await expect(cockpit.getByTestId("runner-storage-donut")).toHaveCount(0);
  await cockpit.getByRole("combobox", { name: "Filter runtime" }).click();
  await page.getByRole("option", { name: "All runtimes", exact: true }).click();
  await expect(chart).toBeVisible();

  await cockpit
    .getByRole("button", { name: /Research image.*Image \+ 2 workspaces/ })
    .click();
  await cockpit
    .getByRole("button", { name: /Ubuntu base.*Image \+ 2 workspaces/ })
    .click();
  await expect(
    cockpit.getByRole("button", { name: "Inspect storage Atlas", exact: true }),
  ).toBeVisible();
  await expect(
    cockpit.getByRole("button", { name: "Inspect storage Cedar", exact: true }),
  ).toBeVisible();
  // Validate native desktop/mobile layout before artifact-only vertical expansion.
  const nativeWidths = await cockpit.evaluate((element) => ({
    width: element.getBoundingClientRect().width,
    parent: element.parentElement!.getBoundingClientRect().width,
    scroll: element.scrollWidth,
    client: element.clientWidth,
  }));
  expect(nativeWidths.width).toBeLessThanOrEqual(nativeWidths.parent + 1);
  expect(nativeWidths.scroll).toBeLessThanOrEqual(nativeWidths.client + 1);
  // Give the settings scroller enough vertical room for an actual panel screenshot.
  // Width remains unchanged, so mobile responsive layout/overflow assertions stay real.
  const viewport = page.viewportSize()!;
  const panelHeight = await cockpit.evaluate(
    (element) => element.getBoundingClientRect().height,
  );
  await page.setViewportSize({
    width: viewport.width,
    height: Math.ceil((panelHeight + 240) / 0.8),
  });
  // Artifact-only vertical expansion removes the modal's 54rem clip, without touching
  // Vue state, horizontal sizing, components or production source files.
  await page.addStyleTag({
    content: `
    [data-slot="dialog-content"] { height: ${panelHeight + 240}px !important; max-height: none !important; top: 0 !important; transform: none !important; translate: -50% 0 !important; }
    [data-slot="dialog-content"] [data-slot="scroll-area-viewport"] { overflow-y: visible !important; }
  `,
  });
  await cockpit.evaluate((element) => {
    let ancestor = element.parentElement;
    while (ancestor) {
      ancestor.scrollTop = 0;
      ancestor = ancestor.parentElement;
    }
  });
  await cockpit.scrollIntoViewIfNeeded();
  await expect(chart).toBeInViewport({ ratio: 1 });
  await expect(map).toBeInViewport({ ratio: 1 });
  await expect(cockpit.getByTestId("runner-storage-donut")).toContainText(
    "Allocated file storage · Cached",
  );
  await expect
    .poll(() => page.evaluate(() => document.fonts.status))
    .toBe("loaded");
  const widths = await cockpit.evaluate((element) => ({
    width: element.getBoundingClientRect().width,
    parent: element.parentElement!.getBoundingClientRect().width,
    scroll: element.scrollWidth,
    client: element.clientWidth,
  }));
  expect(widths.width).toBeLessThanOrEqual(widths.parent + 1);
  expect(widths.scroll).toBeLessThanOrEqual(widths.client + 1);
  mkdirSync(artifacts, { recursive: true });
  const screenshot = resolve(
    artifacts,
    `runner-storage-${testInfo.project.name}.png`,
  );
  await cockpit.screenshot({
    path: screenshot,
    animations: "disabled",
    timeout: 20_000,
  });
  console.info(`Fixture-only real Vue screenshot: ${screenshot}`);
  expect(backend.errors).toEqual([]);
  expect(backend.unexpected).toEqual([]);
});
