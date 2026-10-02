import { mkdirSync } from "node:fs";
import { test, expect, type Page } from "@playwright/test";

const BASE_URL = process.env.E2E_BASE_URL || "http://127.0.0.1:5173";
const COLLAPSED_KEY = "opencuria-sidebar-collapsed-workspaces:1:sidebar-org";
const artifacts = "/workspace/.opencuria/playwright";
mkdirSync(artifacts, { recursive: true });
test.use({
  baseURL: BASE_URL,
  launchOptions: {
    executablePath: process.env.E2E_CHROME_PATH || "/usr/bin/google-chrome",
  },
  viewport: { width: 1440, height: 960 },
  video: "off",
});

const workspace = (
  id: string,
  name: string,
  status = "running",
  runner_online = true,
) => ({
  id,
  name,
  status,
  runner_online,
  runner_id: "runner",
  active_operation: null,
  has_active_session: false,
  runtime_type: "docker",
  credential_ids: [],
  plugin_ids: [],
  credentials_present: false,
  created_by_id: 1,
  created_at: "2026-10-02T10:00:00Z",
  updated_at: "2026-10-02T10:00:00Z",
  last_activity_at: "2026-10-02T10:00:00Z",
  auto_stop_at: null,
  auto_stop_timeout_minutes: null,
  qemu_vcpus: null,
  qemu_memory_mb: null,
  qemu_disk_size_gb: null,
  desktop_width: 1920,
  desktop_height: 1080,
  delete_requested_at: null,
  delete_started_at: null,
  delete_confirmed_at: null,
  delete_last_error: "",
  delete_attempt_count: 0,
});
const chats = (workspace_id: string, workspace_name: string, count: number) =>
  Array.from({ length: count }, (_, index) => ({
    session_id: `${workspace_id}-${index}`,
    workspace_id,
    workspace_name,
    title: `${workspace_name} chat ${index + 1}`,
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "",
    unread: false,
    manual_unread: false,
    needs_attention: false,
    attention_kind: "",
    updated_at: new Date().toISOString(),
    last_message_at: new Date(Date.now() - (index + 1) * 60_000).toISOString(),
  }));

async function installFixtures(page: Page) {
  const workspaces = [
    workspace("zebra", "Zebra"),
    workspace("archive", "Archive", "stopped"),
    workspace("alpha", "Alpha"),
    workspace("empty", "Empty", "stopped"),
  ];
  let conversations = [
    ...chats("alpha", "Alpha", 10),
    ...chats("zebra", "Zebra", 5),
    ...chats("archive", "Archive", 2),
  ];
  conversations[0]!.needs_attention = true;
  conversations[0]!.attention_kind = "permission";
  conversations[1]!.unread = true;
  // Hidden history must not leak into Action required or mark-all-read.
  const archivedChat = conversations.find(
    (row) => row.session_id === "archive-0",
  )!;
  archivedChat.needs_attention = true;
  archivedChat.attention_kind = "permission";
  archivedChat.unread = true;
  const readSessionIds: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const payload = Buffer.from(JSON.stringify({ exp: 4102444800 })).toString(
    "base64url",
  );
  await page.addInitScript((token) => {
    localStorage.setItem("kern_access_token", token);
    localStorage.setItem("kern_refresh_token", "fixture-refresh");
    localStorage.setItem("kern_active_org_id", "sidebar-org");
  }, `fixture.${payload}.signature`);
  let emitSocket: ((event: string, data: unknown) => void) | null = null;
  await page.routeWebSocket(/\/ws\/runner/, (socket) => {
    socket.send(
      "0" +
        JSON.stringify({
          sid: "sidebar-socket",
          upgrades: [],
          pingInterval: 100000,
          pingTimeout: 100000,
        }),
    );
    socket.onMessage((message) => {
      if (String(message).startsWith("40/frontend")) {
        socket.send(
          "40/frontend," + JSON.stringify({ sid: "sidebar-frontend" }),
        );
        emitSocket = (event, data) =>
          socket.send("42/frontend," + JSON.stringify([event, data]));
      } else if (message === "2") socket.send("3");
    });
  });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const method = route.request().method();
    const json = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data });
    if (path === "/auth/me/")
      return json({
        id: 1,
        email: "sidebar@example.test",
        first_name: "Sidebar",
        last_name: "Tester",
        organizations: [
          {
            id: "sidebar-org",
            name: "Sidebar test",
            slug: "sidebar",
            role: "admin",
          },
        ],
      });
    if (path === "/workspaces/") return json(workspaces);
    if (path === "/harness/conversations/") return json(conversations);
    if (path.includes("/harness/sessions/")) {
      const sessionId = path.split("/harness/sessions/")[1]!.split("/")[0]!;
      const chat = conversations.find((row) => row.session_id === sessionId);
      if (chat) {
        if (method === "PATCH") {
          Object.assign(chat, route.request().postDataJSON());
          return json({ ...chat, id: sessionId });
        }
        if (method === "DELETE") {
          conversations = conversations.filter((row) => row !== chat);
          return route.fulfill({ status: 204 });
        }
        if (path.endsWith("/read")) {
          readSessionIds.push(sessionId);
          chat.unread = false;
          chat.manual_unread = false;
          return route.fulfill({ status: 204 });
        }
        if (path.endsWith("/unread")) {
          chat.unread = true;
          chat.manual_unread = true;
          return route.fulfill({ status: 204 });
        }
        if (path.endsWith("/timeline"))
          return json({ session: { ...chat, id: sessionId }, messages: [] });
      }
    }
    const detail = workspaces.find((row) => path === `/workspaces/${row.id}/`);
    if (detail) return json(detail);
    const sessionsWorkspace = workspaces.find(
      (row) => path === `/workspaces/${row.id}/harness/sessions/`,
    );
    if (sessionsWorkspace)
      return json(
        conversations
          .filter((row) => row.workspace_id === sessionsWorkspace.id)
          .map((row) => ({
            ...row,
            id: row.session_id,
            parent_id: null,
            skill_ids: [],
            tokens: {},
            cost: 0,
          })),
      );
    if (path.endsWith("/provider-config/")) return json({});
    return json([]);
  });
  return {
    workspaces,
    readSessionIds,
    errors,
    conversations,
    emit: async (event: string, data: unknown) => {
      await expect.poll(() => emitSocket !== null).toBe(true);
      emitSocket!(event, data);
    },
  };
}

const group = (page: Page, id: string) =>
  page.locator(
    `[data-testid="workspace-conversation-group"][data-workspace-id="${id}"]`,
  );
const groups = (page: Page) => page.getByTestId("workspace-conversation-group");
const collapseToggle = (page: Page, id: string) =>
  group(page, id).getByTestId("workspace-collapse-toggle");

async function expectCollapsed(page: Page, id: string, name: string) {
  const toggle = collapseToggle(page, id);
  await expect(toggle).toHaveAttribute(
    "aria-label",
    `Expand workspace ${name}`,
  );
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(
    group(page, id).getByRole("button", {
      name: `Open workspace ${name}`,
      exact: true,
    }),
  ).toBeVisible();
  await expect(group(page, id).getByTestId("conversation-row")).toHaveCount(0);
  await expect(group(page, id).getByTestId("show-more-chats")).toHaveCount(0);
}

async function expectExpanded(page: Page, id: string, name: string, rows = 4) {
  await expect(collapseToggle(page, id)).toHaveAttribute(
    "aria-label",
    `Collapse workspace ${name}`,
  );
  await expect(collapseToggle(page, id)).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(group(page, id).getByTestId("conversation-row")).toHaveCount(
    rows,
  );
}

async function expectStoredCollapse(page: Page, ids: string[]) {
  await expect
    .poll(() =>
      page.evaluate(
        (key) => JSON.parse(localStorage.getItem(key) ?? "null"),
        COLLAPSED_KEY,
      ),
    )
    .toEqual(ids);
}

async function settledScreenshot(page: Page, filename: string) {
  // Wait for drawer/collapsible animations, not a screenshot-driven action.
  await page.evaluate(async () => {
    await Promise.all(
      document
        .getAnimations()
        .filter(
          (animation) =>
            animation.effect?.getComputedTiming().iterations !== Infinity,
        )
        .map((animation) => animation.finished.catch(() => {})),
    );
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
  });
  await page.screenshot({ path: `${artifacts}/${filename}` });
}

async function rowMenu(page: Page, title: string) {
  const row = page.getByRole("button", {
    name: `Open chat ${title}`,
    exact: true,
  });
  await row.hover();
  await row
    .getByRole("button", { name: `Actions for ${title}`, exact: true })
    .click();
}

test("desktop: workspace order, independent pagination, actions and global search", async ({
  page,
}) => {
  const { errors, readSessionIds, conversations } = await installFixtures(page);
  await page.goto("/");
  await expect(groups(page)).toHaveCount(2);
  expect(
    await groups(page).evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-workspace-id")),
    ),
  ).toEqual(["alpha", "zebra"]);
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(4);
  await expect(
    group(page, "zebra").getByTestId("conversation-row"),
  ).toHaveCount(4);
  await expect(group(page, "archive")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Open chat Archive chat 1", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByTestId("active-section")).toHaveCount(0);
  await expect(page.getByTestId("time-list")).toHaveCount(0);
  await expect(page.getByTestId("workspace-section")).toHaveCount(0);
  await expect(
    page.getByTestId("action-required-section").getByTestId("conversation-row"),
  ).toHaveCount(1);
  await expect(group(page, "alpha").getByTestId("attention-icon")).toHaveCount(
    1,
  );
  await expect(group(page, "alpha").getByTestId("unread-dot")).toHaveCount(1);
  await expect(page.getByTestId("all-workspaces")).toHaveText(
    "All workspaces (4)",
  );
  await page.screenshot({
    path: `${artifacts}/chat-workspace-sidebar-desktop.png`,
  });

  await group(page, "alpha").getByTestId("show-more-chats").click();
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(8);
  await expect(
    group(page, "zebra").getByTestId("conversation-row"),
  ).toHaveCount(4);
  await group(page, "alpha").getByTestId("show-more-chats").click();
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(10);
  await expect(group(page, "alpha").getByTestId("show-more-chats")).toHaveCount(
    0,
  );

  await rowMenu(page, "Zebra chat 2");
  await page.getByRole("menuitem", { name: "Rename", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Rename chat" })
    .fill("Renamed sidebar chat");
  await page.getByRole("textbox", { name: "Rename chat" }).press("Enter");
  await expect(
    page.getByRole("button", {
      name: "Open chat Renamed sidebar chat",
      exact: true,
    }),
  ).toBeVisible();
  await rowMenu(page, "Renamed sidebar chat");
  await page
    .getByRole("menuitem", { name: "Mark as unread", exact: true })
    .click();
  await expect(group(page, "zebra").getByTestId("unread-dot")).toHaveCount(1);
  await page
    .getByRole("button", { name: "Mark all as read", exact: true })
    .click();
  await expect(page.getByTestId("unread-dot")).toHaveCount(0);
  await expect
    .poll(() => [...readSessionIds].sort())
    .toEqual(["alpha-1", "zebra-1"]);
  expect(
    conversations.find((row) => row.session_id === "archive-0")!.unread,
  ).toBe(true);
  await rowMenu(page, "Renamed sidebar chat");
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Delete chat?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Open chat Renamed sidebar chat",
      exact: true,
    }),
  ).toBeVisible();
  await rowMenu(page, "Renamed sidebar chat");
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  await dialog.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Open chat Renamed sidebar chat",
      exact: true,
    }),
  ).toHaveCount(0);

  await page.keyboard.press("Control+k");
  await page.getByTestId("command-palette-input").fill("Alpha chat 10");
  await expect(page.getByTestId("command-palette")).toContainText(
    "Alpha chat 10",
  );
  await page.keyboard.press("Escape");
  expect(errors).toEqual([]);
});

test("older direct link stays visible and reload resets independent expansion", async ({
  page,
}) => {
  const { errors } = await installFixtures(page);
  await page.goto("/workspaces/alpha?session=alpha-8");
  const active = group(page, "alpha").getByRole("button", {
    name: "Open chat Alpha chat 9",
    exact: true,
  });
  await expect(active).toHaveAttribute("aria-selected", "true");
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(10);
  await expect(
    group(page, "zebra").getByTestId("conversation-row"),
  ).toHaveCount(4);
  await group(page, "zebra").getByTestId("show-more-chats").click();
  await expect(
    group(page, "zebra").getByTestId("conversation-row"),
  ).toHaveCount(5);
  await page.reload();
  await expect(active).toHaveAttribute("aria-selected", "true");
  await expect(
    group(page, "zebra").getByTestId("conversation-row"),
  ).toHaveCount(4);
  await active.press("Enter");
  await expect(page).toHaveURL(/workspaces\/alpha\?session=alpha-8/);
  expect(errors).toEqual([]);
});

test("mobile: pagination stays in the drawer; selecting chat closes it", async ({
  page,
}) => {
  const { errors } = await installFixtures(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Toggle Sidebar", exact: true })
    .first()
    .click();
  const drawer = page.getByRole("dialog");
  await expect(drawer).toBeVisible();
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(4);
  await page.screenshot({
    path: `${artifacts}/chat-workspace-sidebar-mobile.png`,
  });
  await group(page, "alpha").getByTestId("show-more-chats").click();
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(8);
  await expect(drawer).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await page
    .getByRole("button", { name: "Toggle Sidebar", exact: true })
    .first()
    .click();
  await expect(
    group(page, "alpha").getByTestId("conversation-row"),
  ).toHaveCount(8);
  await group(page, "alpha")
    .getByRole("button", { name: "Open chat Alpha chat 3", exact: true })
    .click();
  await expect(drawer).toBeHidden();
  await expect(page).toHaveURL(/workspaces\/alpha\?session=alpha-2/);
  expect(errors).toEqual([]);
});

test("live runner/workspace events reorder groups without losing expansion or status timestamp", async ({
  page,
}) => {
  const fixture = await installFixtures(page);
  await page.goto("/");
  await expect(groups(page)).toHaveCount(2);
  await group(page, "alpha").getByTestId("show-more-chats").click();
  await collapseToggle(page, "alpha").click();
  await expectCollapsed(page, "alpha", "Alpha");
  fixture.workspaces.find((row) => row.id === "alpha")!.runner_online = false;
  await fixture.emit("runner:offline", {
    workspace_id: "alpha",
    runner_id: "runner",
  });
  await expect(group(page, "alpha")).toHaveAttribute("data-online", "false");
  expect(
    await groups(page).evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-workspace-id")),
    ),
  ).toEqual(["zebra", "alpha"]);
  await expectCollapsed(page, "alpha", "Alpha");
  fixture.workspaces.find((row) => row.id === "archive")!.status = "running";
  await fixture.emit("workspace:status_changed", {
    workspace_id: "archive",
    status: "running",
    credentials_present: false,
  });
  await expect(group(page, "archive")).toHaveAttribute("data-online", "true");
  expect(
    await groups(page).evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-workspace-id")),
    ),
  ).toEqual(["archive", "zebra", "alpha"]);
  fixture.workspaces.find((row) => row.id === "alpha")!.runner_online = true;
  await fixture.emit("runner:online", {
    workspace_id: "alpha",
    runner_id: "runner",
  });
  await expect(group(page, "alpha")).toHaveAttribute("data-online", "true");
  expect(
    await groups(page).evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-workspace-id")),
    ),
  ).toEqual(["alpha", "archive", "zebra"]);
  await expectCollapsed(page, "alpha", "Alpha");

  await expectStoredCollapse(page, ["alpha"]);
  // Stop removes the whole group; a start recreates it without forgetting collapse.
  fixture.workspaces.find((row) => row.id === "alpha")!.status = "stopped";
  await fixture.emit("workspace:status_changed", {
    workspace_id: "alpha",
    status: "stopped",
    credentials_present: false,
  });
  await expect(group(page, "alpha")).toHaveCount(0);
  await expect(
    page.getByTestId("action-required-section").getByTestId("conversation-row"),
  ).toHaveCount(1);
  fixture.workspaces.find((row) => row.id === "alpha")!.status = "running";
  await fixture.emit("workspace:status_changed", {
    workspace_id: "alpha",
    status: "running",
    credentials_present: false,
  });
  await expectCollapsed(page, "alpha", "Alpha");
  await collapseToggle(page, "alpha").click();
  await expectExpanded(page, "alpha", "Alpha", 8);

  const oldChat = fixture.conversations.find(
    (row) => row.session_id === "alpha-9",
  )!;
  oldChat.status = "busy";
  await fixture.emit("harness.session_status", {
    workspace_id: "alpha",
    session_id: "alpha-9",
    status: "busy",
  });
  await expect(group(page, "alpha").getByTestId("workspace-busy")).toHaveCount(
    1,
  );
  await expect(
    group(page, "alpha").getByRole("button", {
      name: "Open chat Alpha chat 10",
      exact: true,
    }),
  ).toHaveCount(0);
  await group(page, "alpha").getByTestId("show-more-chats").click();
  await expect(
    group(page, "alpha")
      .getByRole("button", { name: "Open chat Alpha chat 10", exact: true })
      .getByTestId("conversation-row-meta"),
  ).toContainText("10m");
  expect(fixture.errors).toEqual([]);
});

test("desktop: separate arrow, native disclosure keyboard and durable browser storage", async ({
  page,
  context,
  browser,
}) => {
  const fixture = await installFixtures(page);
  await page.goto("/");
  await expectExpanded(page, "alpha", "Alpha");
  const toggle = collapseToggle(page, "alpha");
  const controls = await toggle.getAttribute("aria-controls");
  expect(controls).toBeTruthy();
  await expect(page.locator(`[id="${controls}"]`)).toBeVisible();
  const initialUrl = page.url();
  await toggle.click();
  await expectCollapsed(page, "alpha", "Alpha");
  await expect(page.locator(`[id="${controls}"]`)).toBeHidden();
  await expect(page).toHaveURL(initialUrl);
  await expectStoredCollapse(page, ["alpha"]);
  await settledScreenshot(page, "sidebar-collapse-desktop.png");
  await toggle.focus();
  await page.keyboard.press("Space");
  await expectExpanded(page, "alpha", "Alpha");
  await expectStoredCollapse(page, []);
  await page.keyboard.press("Enter");
  await expectCollapsed(page, "alpha", "Alpha");
  await expect(page).toHaveURL(initialUrl);
  await page.reload();
  await expectCollapsed(page, "alpha", "Alpha");
  await expectExpanded(page, "zebra", "Zebra");
  const storageState = await context.storageState();
  expect(fixture.errors).toEqual([]);
  await page.close();

  // A real new page exercises persisted state, not only a component remount.
  const reopened = await context.newPage();
  const reopenedFixture = await installFixtures(reopened);
  await reopened.goto("/");
  await expectCollapsed(reopened, "alpha", "Alpha");
  await collapseToggle(reopened, "alpha").click();
  await expectExpanded(reopened, "alpha", "Alpha");
  await expectStoredCollapse(reopened, []);
  await reopened.reload();
  await expectExpanded(reopened, "alpha", "Alpha");
  expect(reopenedFixture.errors).toEqual([]);

  const restoredContext = await browser.newContext({
    storageState,
    viewport: { width: 1440, height: 960 },
  });
  try {
    const restored = await restoredContext.newPage();
    const restoredFixture = await installFixtures(restored);
    await restored.goto(BASE_URL);
    await expectCollapsed(restored, "alpha", "Alpha");
    await expectStoredCollapse(restored, ["alpha"]);
    expect(restoredFixture.errors).toEqual([]);
  } finally {
    await restoredContext.close();
  }
});

test("selected older direct link and refresh respect collapse, preparing older rows when expanded", async ({
  page,
}) => {
  const fixture = await installFixtures(page);
  await page.goto("/");
  await expectExpanded(page, "alpha", "Alpha");
  await collapseToggle(page, "alpha").click();
  await expectStoredCollapse(page, ["alpha"]);
  await page.goto("/workspaces/alpha?session=alpha-8");
  await expectCollapsed(page, "alpha", "Alpha");
  await page.reload();
  await expectCollapsed(page, "alpha", "Alpha");
  await collapseToggle(page, "alpha").click();
  await expectExpanded(page, "alpha", "Alpha", 10);
  await expect(
    group(page, "alpha").getByRole("button", {
      name: "Open chat Alpha chat 9",
      exact: true,
    }),
  ).toHaveAttribute("aria-selected", "true");
  await expectStoredCollapse(page, []);
  await page.reload();
  await expectExpanded(page, "alpha", "Alpha", 10);
  expect(fixture.errors).toEqual([]);
});

test("mobile: collapse survives drawer remount and arrow never navigates or closes drawer", async ({
  page,
}) => {
  const fixture = await installFixtures(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const openDrawer = async () => {
    await page
      .getByRole("button", { name: "Toggle Sidebar", exact: true })
      .first()
      .click();
    await expect(page.getByRole("dialog")).toBeVisible();
  };
  await openDrawer();
  await expectExpanded(page, "alpha", "Alpha");
  const initialUrl = page.url();
  await collapseToggle(page, "alpha").click();
  await expectCollapsed(page, "alpha", "Alpha");
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page).toHaveURL(initialUrl);
  await settledScreenshot(page, "sidebar-collapse-mobile.png");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await openDrawer();
  await expectCollapsed(page, "alpha", "Alpha");
  await group(page, "alpha")
    .getByRole("button", { name: "Open workspace Alpha", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page).toHaveURL(/\/workspaces\/alpha$/);
  await openDrawer();
  await expectCollapsed(page, "alpha", "Alpha");
  await collapseToggle(page, "alpha").click();
  await expectExpanded(page, "alpha", "Alpha");
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page).toHaveURL(/\/workspaces\/alpha$/);
  await expectStoredCollapse(page, []);
  await page.keyboard.press("Escape");
  await page.reload();
  await openDrawer();
  await expectExpanded(page, "alpha", "Alpha");
  expect(fixture.errors).toEqual([]);
});

test("polling: only running groups survive stop/start, with collapse keyed by workspace id", async ({
  page,
}) => {
  await page.clock.install();
  const fixture = await installFixtures(page);
  // Creating and orphaned history are excluded as well as stopped Archive.
  fixture.workspaces.find((row) => row.id === "empty")!.status = "creating";
  const hiddenHistory = [
    ...chats("missing", "Missing", 1),
    ...chats("empty", "Empty", 1),
  ];
  for (const row of hiddenHistory) {
    row.needs_attention = true;
    row.attention_kind = "permission";
    row.unread = true;
  }
  fixture.conversations.push(...hiddenHistory);
  await page.goto("/");
  await expect(groups(page)).toHaveCount(2);
  await expect(page.getByTestId("all-workspaces")).toHaveText(
    "All workspaces (4)",
  );
  await collapseToggle(page, "alpha").click();
  const alpha = fixture.workspaces.find((row) => row.id === "alpha")!;
  alpha.runner_online = false;
  await page.clock.fastForward(30_000);
  await expect(group(page, "alpha")).toHaveAttribute("data-online", "false");
  await expectCollapsed(page, "alpha", "Alpha");
  expect(
    await groups(page).evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-workspace-id")),
    ),
  ).toEqual(["zebra", "alpha"]);
  alpha.status = "stopped";
  await page.clock.fastForward(30_000);
  await expect(group(page, "alpha")).toHaveCount(0);
  await expect(page.getByTestId("action-required-section")).toHaveCount(0);
  await expectStoredCollapse(page, ["alpha"]);
  alpha.status = "running";
  await page.clock.fastForward(30_000);
  await expectCollapsed(page, "alpha", "Alpha");
  await expect(
    page.getByTestId("action-required-section").getByTestId("conversation-row"),
  ).toHaveCount(1);
  await collapseToggle(page, "alpha").click();
  await expectExpanded(page, "alpha", "Alpha");
  alpha.status = "deleted";
  await page.clock.fastForward(30_000);
  await expect(group(page, "alpha")).toHaveCount(0);
  expect(fixture.errors).toEqual([]);
});
