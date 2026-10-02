import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const apiURL = process.env.E2E_API_URL || "http://127.0.0.1:8000/api/v1";
const statePath =
  process.env.E2E_FIXTURE_STATE || "./test-results/focused.json";
const shots =
  process.env.E2E_SCREENSHOTS_DIR || "/workspace/.opencuria/playwright";
let fixture: any;

type Session = { client: APIRequestContext; headers: Record<string, string> };

async function apiSession(
  email = fixture.adminEmail,
  password = fixture.password,
): Promise<Session> {
  const { request } = await import("@playwright/test");
  const client = await request.newContext({
    baseURL: apiURL.replace(/\/api\/v1\/?$/, ""),
  });
  const login = await client.post("/api/v1/auth/login/", {
    data: { email, password },
  });
  expect(login.ok()).toBe(true);
  const { access_token: token } = await login.json();
  return {
    client,
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Organization-Id": fixture.organizationId,
    },
  };
}

async function loginUI(page: Page): Promise<void> {
  const apiOrigin =
    process.env.E2E_API_ORIGIN || apiURL.replace(/\/api\/v1\/?$/, "");
  const response = await fetch(`${apiOrigin}/api/v1/auth/login/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: fixture.adminEmail,
      password: fixture.password,
    }),
  });
  expect(response.ok).toBe(true);
  const token = await response.json();
  await page.addInitScript(
    ({ access, refresh, organizationId }) => {
      localStorage.setItem("kern_access_token", access);
      localStorage.setItem("kern_refresh_token", refresh);
      localStorage.setItem("kern_active_org_id", organizationId);
    },
    {
      access: token.access_token,
      refresh: token.refresh_token,
      organizationId: fixture.organizationId,
    },
  );
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: fixture.adminEmail }),
  ).toBeVisible();
}

async function settings(
  page: Page,
  tab: "plugins" | "credentials" | "credential-services",
): Promise<void> {
  await page.getByRole("button", { name: fixture.adminEmail }).click();
  await page.getByText("Open settings", { exact: true }).click();
  await page.getByTestId(`settings-nav-${tab}`).last().click();
  await expect(page.getByTestId("settings-sheet-title")).toHaveText(
    tab === "credential-services"
      ? "Credential Services"
      : `${tab[0]!.toUpperCase()}${tab.slice(1)}`,
  );
}

async function saveFixture(): Promise<void> {
  await writeFile(statePath, JSON.stringify(fixture, null, 2), { mode: 0o600 });
}

async function shot(page: Page, name: string): Promise<void> {
  await mkdir(shots, { recursive: true });
  await page.screenshot({ path: path.join(shots, name), fullPage: true });
}

async function browserOAuth(
  page: Page,
  answer: { code?: string; error?: string },
): Promise<string> {
  const authorization = new Promise<string>((resolve) => {
    void page.route(
      "https://issuer.focused-e2e.invalid/authorize**",
      async (route) => {
        const url = route.request().url();
        resolve(url);
        const request = new URL(url);
        const callback = new URL(request.searchParams.get("redirect_uri")!);
        callback.searchParams.set("state", request.searchParams.get("state")!);
        if (answer.error) callback.searchParams.set("error", answer.error);
        if (answer.code) callback.searchParams.set("code", answer.code);
        await route.fulfill({
          status: 302,
          headers: { Location: callback.toString() },
          body: "",
        });
      },
    );
  });
  const callbackPromise = page.waitForResponse((res) =>
    res.url().includes("/api/v1/mcp-oauth/callback/"),
  );
  const connectPromise = page.waitForResponse(
    (res) =>
      res.url().endsWith("/oauth/connect/") &&
      res.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Connect securely" }).click();
  expect((await connectPromise).status()).toBe(200);
  const authURL = new URL(await authorization);
  expect(authURL.hostname).toBe("issuer.focused-e2e.invalid");
  const callback = await callbackPromise;
  expect(callback.status()).toBe(302);
  return new URL(callback.headers().location!).search;
}

test.beforeAll(async () => {
  fixture = JSON.parse(await readFile(statePath, "utf8"));
  await mkdir(shots, { recursive: true });
});

test("plugin overview and detail show activation, human-readable services, and Add Credential", async ({
  page,
}) => {
  await loginUI(page);
  await settings(page, "plugins");
  await expect(page.getByTestId("plugin-catalog")).toBeVisible();
  await shot(page, "plugin-overview.png");
  const card = page.getByTestId(`plugin-open-${fixture.notionPluginId}`);
  await expect(card).toContainText("Active");
  await expect(card).toContainText("1 MCP server");
  await card.click();
  const detail = page.getByTestId("plugin-detail");
  await expect(detail).toContainText("MCP servers (1)");
  await expect(detail).toContainText("https://mcp.notion.com/mcp");
  await expect(detail).toContainText("Credential services (1)");
  await expect(detail).toContainText("OAuth");
  await expect(detail).not.toContainText("mcp_oauth");
  await shot(page, "plugin-detail.png");
  await page
    .getByTestId(`plugin-add-credential-${fixture.notionServiceId}`)
    .click();
  const dialog = page.getByRole("dialog").last();
  await expect(dialog).toContainText("Connect Notion OAuth");
  await expect(dialog.getByText("Value", { exact: true })).toHaveCount(0);
  await shot(page, "credential-oauth-prefill.png");
});

test("credential services round-trip service_id through a plugin and survive plugin deletion", async ({
  page,
}) => {
  await loginUI(page);
  await settings(page, "credential-services");
  const name = `${fixture.marker} isolated OAuth service`;
  const endpoint = "https://mcp.fixture.example.test/endpoint";
  await page.getByRole("button", { name: "New Service" }).click();
  const serviceDialog = page.getByRole("dialog").last();
  await serviceDialog.getByLabel("Name", { exact: true }).fill(name);
  await serviceDialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "MCP OAuth" }).click();
  await serviceDialog.getByLabel("OAuth MCP server endpoint").fill(endpoint);
  const serviceCreated = page.waitForResponse(
    (res) =>
      res.url().endsWith("/org-credential-services/") &&
      res.request().method() === "POST",
  );
  await serviceDialog.getByRole("button", { name: "Create Service" }).click();
  const response = await serviceCreated;
  expect(response.status()).toBe(201);
  const service = await response.json();
  expect(service).toMatchObject({
    name,
    credential_type: "mcp_oauth",
    oauth_server_url: endpoint,
  });
  fixture.createdServices = [...(fixture.createdServices || []), service.id];
  await saveFixture();

  await page.getByTestId("settings-nav-plugins").last().click();
  await page.getByTestId("plugin-create").click();
  const editor = page.getByTestId("plugin-editor-dialog");
  const pluginName = `${fixture.marker} service binding plugin`;
  await editor.getByTestId("plugin-name").fill(pluginName);
  await editor.getByTestId("plugin-add-requirement").click();
  const requirement = editor.getByTestId("plugin-requirement-0");
  await requirement.getByTestId("plugin-requirement-service-0").click();
  await page.getByRole("option", { name: new RegExp(name) }).click();
  await requirement.getByLabel("Key").fill("bound_oauth");
  await editor.getByTestId("plugin-add-mcp").click();
  const mcp = editor.getByTestId("plugin-mcp-0");
  await mcp.getByLabel("Name").fill("Bound service MCP");
  await mcp.getByTestId("plugin-mcp-transport-0").click();
  await page.getByRole("option", { name: /streamable_http/ }).click();
  await mcp.getByTestId("plugin-mcp-auth-0").click();
  await page.getByRole("option", { name: /OAuth 2.0 \(MCP\)/ }).click();
  await mcp.getByTestId("plugin-mcp-oauth-requirement-0").click();
  await page.getByRole("option", { name: "bound_oauth" }).click();
  await expect(mcp.getByLabel("URL")).toHaveValue(endpoint);
  const createdPluginResponse = page.waitForResponse(
    (res) =>
      res.url().endsWith("/plugins/") && res.request().method() === "POST",
  );
  await editor.getByTestId("plugin-editor-save").click();
  const createdPlugin = await createdPluginResponse;
  expect(createdPlugin.status()).toBe(201);
  const plugin = await createdPlugin.json();
  expect(plugin.credential_requirements).toEqual([
    expect.objectContaining({
      service_id: service.id,
      credential_type: "mcp_oauth",
    }),
  ]);
  fixture.createdPlugins = [...(fixture.createdPlugins || []), plugin.id];
  await saveFixture();

  const api = await apiSession();
  try {
    const stored = await api.client.get(`/api/v1/plugins/${plugin.id}/`, {
      headers: api.headers,
    });
    expect(stored.status()).toBe(200);
    expect((await stored.json()).credential_requirements[0].service_id).toBe(
      service.id,
    );
    const pluginDelete = await api.client.delete(
      `/api/v1/plugins/${plugin.id}/`,
      { headers: api.headers },
    );
    expect(pluginDelete.status()).toBe(204);
    const services = await api.client
      .get("/api/v1/credential-services/", { headers: api.headers })
      .then((res) => res.json());
    expect(services.some((entry: any) => entry.id === service.id)).toBe(true);
  } finally {
    await api.client.dispose();
  }
});

test("credential creation scopes, API visibility, and member permissions use live APIs", async ({
  page,
}) => {
  await loginUI(page);
  await settings(page, "credentials");
  const session = await apiSession();
  try {
    const services = await session.client
      .get("/api/v1/credential-services/", { headers: session.headers })
      .then((r) => r.json());
    const github = services.find((service: any) => service.slug === "github");
    expect(github?.is_active).toBe(true);
    const ids: string[] = [];
    for (const [name, shared] of [
      [`${fixture.marker} personal token`, false],
      [`${fixture.marker} shared token`, true],
    ] as const) {
      await page.getByTestId("credentials-add-trigger").click();
      const dialog = page.getByRole("dialog").last();
      await dialog.getByRole("combobox").click();
      await page.getByRole("option", { name: "GitHub" }).click();
      await dialog.getByLabel("Name").fill(name);
      await dialog
        .getByPlaceholder(/Value for GITHUB_TOKEN/)
        .fill("focused-test-only-secret");
      if (shared)
        await dialog
          .getByRole("switch", { name: "Share with entire organization" })
          .click();
      const created = page.waitForResponse(
        (res) =>
          res.url().endsWith("/credentials/") &&
          res.request().method() === "POST",
      );
      await dialog.getByRole("button", { name: "Save Credential" }).click();
      const response = await created;
      expect(response.status()).toBe(201);
      const credential = await response.json();
      expect(credential.scope).toBe(shared ? "organization" : "personal");
      ids.push(credential.id);
    }
    fixture.createdCredentials = [
      ...(fixture.createdCredentials || []),
      ...ids,
    ];
    await saveFixture();
    const member = await session.client.post("/api/v1/auth/login/", {
      data: { email: fixture.memberEmail, password: fixture.memberPassword },
    });
    const headers = {
      Authorization: `Bearer ${(await member.json()).access_token}`,
      "X-Organization-Id": fixture.organizationId,
    };
    const visible = await session.client
      .get("/api/v1/credentials/", { headers })
      .then((r) => r.json());
    expect(visible.some((credential: any) => credential.id === ids[0])).toBe(
      false,
    );
    expect(visible.some((credential: any) => credential.id === ids[1])).toBe(
      true,
    );
  } finally {
    await session.client.dispose();
  }
});

test("real OAuth callback resumes an edit draft without attaching the new account automatically", async ({
  page,
}) => {
  await loginUI(page);
  await page.goto("/workspaces");
  await page
    .getByPlaceholder("Search by name or ID...")
    .fill(fixture.workspaceName);
  const card = page
    .locator("div.flex.flex-col.gap-2")
    .filter({ hasText: fixture.workspaceName });
  await card.getByTitle("Edit workspace").click();
  const dialog = page.getByRole("dialog").last();
  const draftName = `${fixture.workspaceName} callback draft`;
  await dialog.getByPlaceholder("Workspace name").fill(draftName);
  await dialog.getByRole("button", { name: /^Notion\s/ }).click();
  await dialog
    .getByRole("button", { name: "Add Notion OAuth credential" })
    .click();
  const credentialDialog = page.getByRole("dialog").last();
  await credentialDialog
    .getByLabel("Name")
    .fill(`${fixture.marker} edit callback account`);
  const query = await browserOAuth(page, { code: "focused-edit-oauth-code" });
  expect(query).toContain("oauth_result=connected");
  const id = new URLSearchParams(query).get("credential_id")!;
  fixture.oauthCredentials.push(id);
  fixture.oauthNames.push(`${fixture.marker} edit callback account`);
  await saveFixture();
  await expect(page.getByTestId(`oauth-credential-${id}`)).toContainText(
    "Connected",
  );
  await page.getByTestId("workspace-draft-back").click();
  const resumed = page.getByRole("dialog").last();
  await expect(resumed.getByPlaceholder("Workspace name")).toHaveValue(
    draftName,
  );
  await expect(
    resumed.getByRole("button", { name: /^Notion\s/ }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    resumed.getByText(`Selected: ${fixture.oauthNames.at(-1)}`, {
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(resumed.getByTestId("edit-workspace-save")).toBeDisabled();
  await resumed
    .getByRole("button", { name: fixture.oauthNames.at(-1), exact: true })
    .click();
  const patchPromise = page.waitForResponse(
    (res) =>
      res.url().endsWith(`/workspaces/${fixture.workspaceId}/`) &&
      res.request().method() === "PATCH",
  );
  await resumed.getByTestId("edit-workspace-save").click();
  const patch = await patchPromise;
  expect(patch.status()).toBe(200);
  expect(patch.request().postDataJSON()).toMatchObject({
    plugin_ids: [fixture.notionPluginId],
    credential_ids: [id],
  });
});

test("create draft survives OAuth denial and retry, then persists image, repositories and explicit selections", async ({
  page,
}) => {
  await loginUI(page);
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "Create Workspace" }).click();
  let draft = page.getByRole("dialog").last();
  const name = `${fixture.marker} OAuth created workspace`;
  await draft.getByPlaceholder("My workspace").fill(name);
  await draft.getByTestId("create-image-select").click();
  await page
    .getByRole("option", {
      name: new RegExp(`${fixture.marker} Fixture Image`),
    })
    .click();
  await draft
    .getByPlaceholder("https://github.com/owner/repo")
    .fill("https://github.com/example/repo");
  await draft.getByRole("button", { name: "Add", exact: true }).click();
  await draft.getByRole("button", { name: /^Notion\s/ }).click();
  await draft
    .getByRole("button", { name: "Add Notion OAuth credential" })
    .click();
  let credentialDialog = page.getByRole("dialog").last();
  const workspaceDraftId = await page.evaluate(() =>
    sessionStorage.getItem("opencuria:workspace-draft-active"),
  );
  expect(workspaceDraftId).toBeTruthy();
  await credentialDialog
    .getByLabel("Name")
    .fill(`${fixture.marker} denied account`);
  expect(await browserOAuth(page, { error: "access_denied" })).toContain(
    "oauth_result=error",
  );
  await expect(page.getByTestId("settings-sheet-title")).toHaveText(
    "Credentials",
  );
  await expect(page.getByTestId("workspace-draft-back")).toBeVisible();
  await page.getByTestId("workspace-draft-back").click();
  draft = page.getByRole("dialog").last();
  await expect(draft.getByPlaceholder("My workspace")).toHaveValue(name);
  await draft
    .getByRole("button", { name: "Add Notion OAuth credential" })
    .click();
  credentialDialog = page.getByRole("dialog").last();
  const accountName = `${fixture.marker} browser Notion account`;
  await credentialDialog.getByLabel("Name").fill(accountName);
  await shot(page, "workspace-create-draft.png");
  const query = await browserOAuth(page, { code: "focused-create-oauth-code" });
  expect(query).toContain("oauth_result=connected");
  const id = new URLSearchParams(query).get("credential_id")!;
  fixture.oauthCredentials.push(id);
  fixture.oauthNames.push(accountName);
  await expect(page.getByTestId("workspace-draft-back")).toBeVisible();
  const verifyConnection = await apiSession();
  try {
    const credentials = await verifyConnection.client
      .get("/api/v1/credentials/", { headers: verifyConnection.headers })
      .then((res) => res.json());
    const connected = credentials.find(
      (credential: any) => credential.id === id,
    );
    expect(connected).toMatchObject({
      name: accountName,
      credential_type: "mcp_oauth",
      oauth_connected: true,
    });
    expect(JSON.stringify(connected)).not.toContain(
      "test-only-provider-access-token",
    );
    expect(JSON.stringify(connected)).not.toContain(
      "test-only-provider-refresh-token",
    );
  } finally {
    await verifyConnection.client.dispose();
  }
  await saveFixture();
  await page.getByTestId("workspace-draft-back").click();
  draft = page.getByRole("dialog").last();
  await expect(draft.getByPlaceholder("My workspace")).toHaveValue(name);
  await expect(draft.getByText("repo", { exact: true })).toBeVisible();
  await expect(
    draft.getByRole("button", { name: /^Notion\s/ }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    draft.getByText(`Selected: ${accountName}`, { exact: true }),
  ).toHaveCount(0);
  await draft.getByRole("button", { name: accountName, exact: true }).click();
  await expect(
    draft.getByRole("button", { name: "Create", exact: true }),
  ).toBeEnabled();
  const createdPromise = page.waitForResponse(
    (res) =>
      res.url().endsWith("/workspaces/") && res.request().method() === "POST",
  );
  await draft.getByRole("button", { name: "Create", exact: true }).click();
  const response = await createdPromise;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON()).toMatchObject({
    name,
    repos: ["https://github.com/example/repo"],
    plugin_ids: [fixture.notionPluginId],
    credential_ids: [id],
    image_artifact_id: fixture.imageArtifactId,
  });
  const workspace = await response.json();
  fixture.createdWorkspaces = [
    ...(fixture.createdWorkspaces || []),
    workspace.workspace_id,
  ];
  await saveFixture();
  const session = await apiSession();
  try {
    const stored = await session.client.get(
      `/api/v1/workspaces/${workspace.workspace_id}/`,
      { headers: session.headers },
    );
    expect(stored.status()).toBe(200);
    expect(await stored.json()).toMatchObject({
      name,
      plugin_ids: [fixture.notionPluginId],
      credential_ids: [id],
    });
  } finally {
    await session.client.dispose();
  }
});

test("workspace edit sends the final plugin and credential selection in one PATCH", async ({
  page,
}) => {
  await loginUI(page);
  const session = await apiSession();
  try {
    await session.client.patch(`/api/v1/workspaces/${fixture.workspaceId}/`, {
      headers: session.headers,
      data: { name: fixture.workspaceName, plugin_ids: [], credential_ids: [] },
    });
  } finally {
    await session.client.dispose();
  }
  await page.goto("/workspaces");
  await page
    .getByPlaceholder("Search by name or ID...")
    .fill(fixture.workspaceName);
  const card = page
    .locator("div.flex.flex-col.gap-2")
    .filter({ hasText: fixture.workspaceName });
  await card.getByTitle("Edit workspace").click();
  const dialog = page.getByRole("dialog").last();
  await dialog
    .getByPlaceholder("Workspace name")
    .fill(`${fixture.workspaceName} configured`);
  await dialog.getByRole("button", { name: /^Notion\s/ }).click();
  await dialog
    .getByRole("button", { name: fixture.oauthNames[0], exact: true })
    .first()
    .click();
  await shot(page, "workspace-edit-selected.png");
  const patchPromise = page.waitForResponse(
    (res) =>
      res.url().endsWith(`/workspaces/${fixture.workspaceId}/`) &&
      res.request().method() === "PATCH",
  );
  await dialog.getByTestId("edit-workspace-save").click();
  const patch = await patchPromise;
  expect(patch.status()).toBe(200);
  expect(patch.request().postDataJSON()).toMatchObject({
    name: `${fixture.workspaceName} configured`,
    plugin_ids: [fixture.notionPluginId],
    credential_ids: [fixture.oauthCredentials[0]],
  });
});

test("mobile plugin detail stays within the viewport and has a usable back control", async ({
  browser,
}) => {
  const session = await apiSession();
  try {
    const login = await session.client.post("/api/v1/auth/login/", {
      data: { email: fixture.adminEmail, password: fixture.password },
    });
    const tokens = await login.json();
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
    });
    await context.addInitScript(
      ({ access, refresh, organizationId }) => {
        localStorage.setItem("kern_access_token", access);
        localStorage.setItem("kern_refresh_token", refresh);
        localStorage.setItem("kern_active_org_id", organizationId);
      },
      {
        access: tokens.access_token,
        refresh: tokens.refresh_token,
        organizationId: fixture.organizationId,
      },
    );
    const page = await context.newPage();
    await page.goto("/?settings=plugins");
    await expect(page.getByTestId("settings-sheet-title")).toHaveText(
      "Plugins",
    );
    await page.getByTestId(`plugin-open-${fixture.notionPluginId}`).click();
    const width = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
    }));
    expect(width.scroll).toBeLessThanOrEqual(width.viewport);
    await shot(page, "plugin-detail-mobile.png");
    await page.getByTestId("plugin-back").click();
    await expect(page.getByTestId("plugin-catalog")).toBeVisible();
    await context.close();
  } finally {
    await session.client.dispose();
  }
});
