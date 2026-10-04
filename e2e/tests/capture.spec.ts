import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";

const artifacts = resolve(__dirname, "../../..", ".opencuria/playwright");
mkdirSync(artifacts, { recursive: true });

/** Artifact capture is bounded separately from UI assertions, never best-effort. */
async function captureScreenshot(page: Page, filename: string) {
  const started = Date.now();
  await expect
    .poll(() => page.evaluate(() => document.fonts.status), {
      timeout: 15_000,
      message: "Screenshot fonts must finish loading",
    })
    .toBe("loaded");
  await page.screenshot({
    path: `${artifacts}/${filename}`,
    animations: "disabled",
    timeout: 20_000,
  });
  console.info(
    `Screenshot ${filename}: ${Date.now() - started}ms including font readiness`,
  );
}

/** Only the backend is simulated: Vue, router, stores and components are real. */
async function installCaptureBackend(page: Page) {
  const errors: string[] = [];
  const unexpected: string[] = [];
  const posts: unknown[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const workspace = {
    id: "capture-vm",
    name: "Capture VM",
    status: "running",
    runner_online: true,
    runner_id: "capture-runner",
    active_operation: null as string | null,
    has_active_session: false,
    runtime_type: "qemu",
    credential_ids: [],
    plugin_ids: [],
    credentials_present: false,
    intervention_required: false,
    created_by_id: 1,
    created_at: "2026-10-03T10:00:00Z",
    updated_at: "2026-10-03T10:00:00Z",
    last_activity_at: "2026-10-03T10:00:00Z",
    auto_stop_at: null,
    auto_stop_timeout_minutes: null,
    qemu_vcpus: 2,
    qemu_memory_mb: 2048,
    qemu_disk_size_gb: 20,
    desktop_width: 1920,
    desktop_height: 1080,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: "",
    delete_attempt_count: 0,
  };
  const session = {
    id: "saved-chat",
    session_id: "saved-chat",
    workspace_id: workspace.id,
    workspace_name: workspace.name,
    title: "Saved capture history",
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "",
    parent_id: null,
    skill_ids: [],
    tokens: {},
    cost: 0,
    unread: false,
    manual_unread: false,
    needs_attention: false,
    attention_kind: "",
    updated_at: "2026-10-03T10:00:00Z",
    last_message_at: "2026-10-03T10:00:00Z",
  };
  const token = Buffer.from(JSON.stringify({ exp: 4102444800 })).toString(
    "base64url",
  );
  await page.addInitScript((token) => {
    localStorage.setItem("kern_access_token", token);
    localStorage.setItem("kern_refresh_token", "capture-refresh");
    localStorage.setItem("kern_active_org_id", "capture-org");
  }, `fixture.${token}.signature`);
  let emitSocket: ((event: string, data: unknown) => void) | undefined;
  await page.routeWebSocket(/\/ws\/runner/, (socket) => {
    socket.send(
      "0" +
        JSON.stringify({
          sid: "capture-socket",
          upgrades: [],
          pingInterval: 100000,
          pingTimeout: 100000,
        }),
    );
    socket.onMessage((message) => {
      if (String(message).startsWith("40/frontend")) {
        socket.send(
          "40/frontend," + JSON.stringify({ sid: "capture-frontend" }),
        );
        emitSocket = (event, data) =>
          socket.send("42/frontend," + JSON.stringify([event, data]));
      } else if (message === "2") socket.send("3");
    });
  });
  let pending: Route | undefined;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const method = route.request().method();
    const json = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data });
    if (
      method === "POST" &&
      ["/workspaces/capture-vm/image-artifacts/", "/image-artifacts/"].includes(
        path,
      )
    ) {
      posts.push(route.request().postDataJSON());
      pending = route; // Explicitly defer acceptance to prove the optimistic fence.
      return;
    }
    if (method === "GET") {
      if (path === "/auth/me/")
        return json({
          id: 1,
          email: "capture@example.test",
          first_name: "Capture",
          last_name: "Tester",
          organizations: [
            {
              id: "capture-org",
              name: "Capture test",
              slug: "capture",
              role: "admin",
            },
          ],
        });
      if (path === "/workspaces/") return json([workspace]);
      if (path === "/workspaces/capture-vm/") return json(workspace);
      if (path === "/runners/")
        return json([
          {
            id: "capture-runner",
            name: "Capture runner",
            status: "online",
            available_runtimes: ["qemu"],
          },
        ]);
      if (path === "/harness/conversations/") return json([session]);
      if (path === "/workspaces/capture-vm/harness/sessions/")
        return json([session]);
      if (path === "/harness/sessions/saved-chat/timeline")
        return json({ session, messages: [] });
      if (path === "/provider-config/") return json({});
      if (
        [
          "/scheduled-tasks/",
          "/image-definitions/",
          "/skills/",
          "/provider-config/models/",
          "/recent-models/",
          "/agent-configs/",
          "/provider-config/providers/",
          "/image-artifacts/",
          "/harness/sessions/saved-chat/todos",
        ].includes(path)
      )
        return json([]);
    }
    if (method === "POST" && path === "/harness/sessions/saved-chat/read")
      return route.fulfill({ status: 204 });
    unexpected.push(`${method} ${path}`);
    return json(
      { detail: `Unexpected capture fixture request: ${method} ${path}` },
      501,
    );
  });
  return {
    posts,
    errors,
    unexpected,
    workspace,
    rejectBusy: async () => {
      await expect.poll(() => !!pending).toBe(true);
      await pending!.fulfill({
        status: 409,
        json: { detail: "Workspace has an active agent session" },
      });
      pending = undefined;
    },
    accept: async () => {
      await expect.poll(() => !!pending).toBe(true);
      await pending!.fulfill({
        status: 202,
        json: { task_id: "capture-task", workspace_id: workspace.id },
      });
      pending = undefined;
    },
    emit: async (event: string, data: unknown) => {
      await expect.poll(() => !!emitSocket).toBe(true);
      emitSocket!(event, { workspace_id: workspace.id, ...(data as object) });
    },
  };
}

async function expectFenced(page: Page) {
  await expect(page.getByTestId("workspace-chat-header-status")).toContainText(
    "Capturing",
  );
  for (const id of [
    "new-chat",
    "toggle-processes",
    "more",
    "toggle-side-panel",
    "name",
  ]) {
    await expect(
      page.getByTestId(`workspace-chat-header-${id}`),
    ).toBeDisabled();
  }
  await expect(page.getByTestId("composer-textarea")).toHaveAttribute(
    "contenteditable",
    "false",
  );
  await expect(page.getByTestId("composer-textarea")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await expect(page.getByTestId("composer-send")).toBeDisabled();
  await expect(page.getByTestId("composer-mode-trigger")).toBeDisabled();
}

async function expectAutomaticDialog(page: Page, ready = true) {
  const dialog = page.getByRole("dialog", {
    name: "Capture Image",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("automatically stops and restarts");
  await expect(dialog.getByRole("checkbox")).toHaveCount(0);
  await expect(dialog.getByText(/approve|approval/i)).toHaveCount(0);
  await dialog.getByPlaceholder("e.g. before-refactor").fill("before-refactor");
  if (ready)
    await expect(
      dialog.getByRole("button", { name: "Capture Image", exact: true }),
    ).toBeEnabled();
  return dialog;
}

test("workspace capture fences optimistically, survives internal status changes, history and REST reload", async ({
  page,
}) => {
  const backend = await installCaptureBackend(page);
  await page.goto("/workspaces/capture-vm");
  await expect(page.getByTestId("workspace-chat-header-more")).toBeEnabled();
  await page.getByTestId("workspace-chat-header-more").click();
  await page.getByTestId("workspace-chat-header-capture-image").click();
  const dialog = await expectAutomaticDialog(page);
  await dialog
    .getByRole("button", { name: "Capture Image", exact: true })
    .click();
  await expect.poll(() => backend.posts.length).toBe(1);
  expect(backend.posts).toEqual([{ name: "before-refactor" }]);
  // No WS event and no POST response yet: this must be the optimistic label.
  await expect(page.getByTestId("workspace-chat-header-status")).toContainText(
    "Capturing",
  );
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expectFenced(page);
  backend.workspace.active_operation = "capturing_image";
  await backend.emit("workspace:operation_changed", {
    active_operation: "capturing_image",
  });
  await backend.accept();
  await expectFenced(page);
  backend.workspace.status = "stopped";
  await backend.emit("workspace:status_changed", {
    status: "stopped",
    credentials_present: false,
  });
  await expectFenced(page);
  await page
    .getByRole("button", {
      name: "Open chat Saved capture history",
      exact: true,
    })
    .click();
  await expect(page).toHaveURL(/session=saved-chat/);
  await expect(page.getByTestId("workspace-chat-header-chat-title")).toHaveText(
    "Saved capture history",
  );
  await expectFenced(page);
  await page.reload();
  await expectFenced(page); // Fresh store restores the operation from REST, no new WS start.
  await captureScreenshot(page, "capture.png");
  backend.workspace.status = "running";
  await backend.emit("workspace:status_changed", {
    status: "running",
    credentials_present: false,
  });
  await expectFenced(page);
  backend.workspace.active_operation = null;
  await backend.emit("workspace:operation_changed", { active_operation: null });
  await expect(
    page.getByTestId("workspace-chat-header-status"),
  ).not.toContainText("Capturing");
  await expect(page.getByTestId("workspace-chat-header-more")).toBeEnabled();
  await expect(page.getByTestId("composer-textarea")).toHaveAttribute(
    "contenteditable",
    "true",
  );
  expect(backend.posts).toHaveLength(1);
  expect(backend.unexpected).toEqual([]);
  expect(backend.errors).toEqual([]);
});

test("global Images capture dialog explains automatic restart without approval", async ({
  page,
}) => {
  const backend = await installCaptureBackend(page);
  await page.goto("/images");
  await page
    .getByRole("button", { name: "Capture Image", exact: true })
    .click();
  const dialog = await expectAutomaticDialog(page, false);
  await dialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "Capture VM", exact: true }).click();
  const submit = dialog.getByRole("button", {
    name: "Capture Image",
    exact: true,
  });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect.poll(() => backend.posts.length).toBe(1);
  expect(backend.posts).toEqual([
    { name: "before-refactor", workspace_id: "capture-vm" },
  ]);
  backend.workspace.active_operation = "capturing_image";
  await backend.accept();
  await expect(dialog).not.toBeVisible();
  expect(backend.unexpected).toEqual([]);
  expect(backend.errors).toEqual([]);

  // Verify the POST first. Reopen with a capturable fixture for the pre-submit
  // artifact so screenshot work cannot prevent submission coverage.
  backend.workspace.active_operation = null;
  // /images redirects to the settings overlay; reload loses that overlay.
  await page.goto("/images");
  await page
    .getByRole("button", { name: "Capture Image", exact: true })
    .click();
  const artifactDialog = await expectAutomaticDialog(page, false);
  await artifactDialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "Capture VM", exact: true }).click();
  await expect(
    artifactDialog.getByRole("button", { name: "Capture Image", exact: true }),
  ).toBeEnabled();
  await expect(page.getByRole("option")).toHaveCount(0);
  await captureScreenshot(page, "capture-dialog.png");
  expect(backend.posts).toHaveLength(1);
  expect(backend.unexpected).toEqual([]);
  expect(backend.errors).toEqual([]);
});

for (const entry of ["workspace", "global"] as const) {
  test(`${entry} capture rejects a busy agent without leaving a stuck fence`, async ({
    page,
  }) => {
    const backend = await installCaptureBackend(page);
    await page.goto(
      entry === "workspace" ? "/workspaces/capture-vm" : "/images",
    );
    if (entry === "workspace") {
      await page.getByTestId("workspace-chat-header-more").click();
      await page.getByTestId("workspace-chat-header-capture-image").click();
    } else {
      await page
        .getByRole("button", { name: "Capture Image", exact: true })
        .click();
    }
    const dialog = await expectAutomaticDialog(page, entry === "workspace");
    if (entry === "global") {
      await dialog.getByRole("combobox").click();
      await page
        .getByRole("option", { name: "Capture VM", exact: true })
        .click();
    }
    const submit = dialog.getByRole("button", {
      name: "Capture Image",
      exact: true,
    });
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect.poll(() => backend.posts.length).toBe(1);
    expect(backend.posts[0]).toEqual(
      entry === "workspace"
        ? { name: "before-refactor" }
        : { name: "before-refactor", workspace_id: "capture-vm" },
    );
    await backend.rejectBusy();
    await expect(dialog.getByRole("alert")).toBeVisible();
    await expect(submit).toBeEnabled();
    await expect(dialog.getByPlaceholder("e.g. before-refactor")).toBeEnabled();
    await captureScreenshot(page, `capture-${entry}-busy.png`);
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    if (entry === "global") await page.goto("/workspaces/capture-vm");
    await expect(page.getByTestId("workspace-chat-header-more")).toBeEnabled();
    await expect(page.getByTestId("composer-textarea")).toHaveAttribute(
      "contenteditable",
      "true",
    );
    await expect(
      page.getByTestId("workspace-chat-header-status"),
    ).not.toContainText("Capturing");
    expect(backend.posts).toHaveLength(1);
    expect(backend.unexpected).toEqual([]);
    expect(backend.errors).toEqual([]);
  });
}

test("operation events preserve unknown recovery flags until explicit recovery", async ({
  page,
}) => {
  const backend = await installCaptureBackend(page);
  await page.goto("/workspaces/capture-vm");
  await expect(page.getByTestId("workspace-chat-header-more")).toBeEnabled();
  await backend.emit("workspace:operation_changed", {
    active_operation: null,
    intervention_required: true,
    lifecycle_diagnostic: "capture_restart_failed",
  });
  await expect(page.getByTestId("workspace-chat-header-status")).toContainText(
    "Needs intervention",
  );
  await expect(page.getByTestId("workspace-chat-header-more")).toBeDisabled();
  await expect(page.getByTestId("composer-textarea")).toHaveAttribute(
    "contenteditable",
    "false",
  );
  // Older publishers may omit recovery flags. A null operation alone is not proof of recovery.
  await backend.emit("workspace:operation_changed", { active_operation: null });
  await expect(page.getByTestId("workspace-chat-header-status")).toContainText(
    "Needs intervention",
  );
  await expect(page.getByTestId("workspace-chat-header-more")).toBeDisabled();
  await backend.emit("workspace:operation_changed", {
    active_operation: null,
    intervention_required: false,
    lifecycle_diagnostic: "",
  });
  await expect(page.getByTestId("workspace-chat-header-more")).toBeEnabled();
  await expect(page.getByTestId("composer-textarea")).toHaveAttribute(
    "contenteditable",
    "true",
  );
  expect(backend.posts).toEqual([]);
  expect(backend.unexpected).toEqual([]);
  expect(backend.errors).toEqual([]);
});
