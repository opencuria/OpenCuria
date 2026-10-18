import { test, expect, type Browser, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

const state = JSON.parse(
  readFileSync(
    process.env.E2E_PROVIDER_STATE ||
      "./test-results/provider-accounts/state.json",
    "utf8",
  ),
);
const password = "Provider-E2E-2026!";

async function login(browser: Browser, role: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/login");
  await page
    .getByLabel("Email", { exact: true })
    .fill(`provider-${role}@example.test`);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/);
  return { page, context };
}

async function api(page: Page, path: string, method = "GET", body?: unknown) {
  return page.evaluate(
    async ({ path, method, body }) => {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${localStorage.getItem("kern_access_token")}`,
        "X-Organization-Id": localStorage.getItem("kern_active_org_id")!,
        "Content-Type": "application/json",
      };
      const r = await fetch(`/api/v1${path}`, {
        method,
        headers,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      return {
        status: r.status,
        body: r.status === 204 ? null : await r.json(),
      };
    },
    { path, method, body },
  );
}

async function activeUse(page: Page, label: string) {
  await page.goto("/?settings=provider");
  await page.getByTestId("provider-manage-openai-compatible").click();
  const dialog = page.getByRole("dialog").last();
  const row = dialog
    .locator("div.flex.items-center")
    .filter({ has: page.getByText(label, { exact: true }) });
  if (await row.getByRole("button", { name: "Use", exact: true }).count())
    await row.getByRole("button", { name: "Use", exact: true }).click();
  await expect(row.getByText("Active", { exact: true })).toBeVisible();
}

async function settled(page: Page, id: string) {
  let envelope: any;
  await expect
    .poll(
      async () => {
        envelope = (await api(page, `/harness/sessions/${id}/timeline`)).body;
        return envelope.messages?.at(-1)?.finish || "";
      },
      { timeout: 30_000 },
    )
    .not.toBe("");
  return envelope.messages.at(-1);
}

test("member creates named accounts, selects none, switches between turns and revokes without fallback", async ({
  browser,
}) => {
  const { page, context } = await login(browser, "member");
  const prefix = `UI ${randomUUID().slice(0, 8)}`;
  await page.goto("/?settings=provider");
  await page.getByTestId("provider-manage-openai-compatible").click();
  const dialog = page.getByRole("dialog").last();
  for (const suffix of ["A", "B"]) {
    await page.getByTestId("add-provider-account").click();
    await page
      .getByLabel("Account name", { exact: true })
      .fill(`${prefix} ${suffix}`);
    await page
      .getByLabel("Base URL", { exact: true })
      .fill("http://127.0.0.1:8099/v1");
    await page
      .getByLabel("API key (optional)", { exact: true })
      .fill(`fixture-${prefix}-${suffix}`);
    await page
      .getByLabel("Models (one per line)", { exact: true })
      .fill("local/demo");
    await expect(page.getByTestId("account-shared")).toHaveCount(0);
    await page.getByTestId("save-provider-account").click();
    await expect(
      dialog.getByText(`${prefix} ${suffix}`, { exact: true }),
    ).toBeVisible();
  }
  await activeUse(page, `${prefix} A`);
  const accounts = (await api(page, "/provider-config/accounts/")).body;
  const first = accounts.find((a: any) => a.label === `${prefix} A`);
  const second = accounts.find((a: any) => a.label === `${prefix} B`);
  await page.getByTestId("select-no-account").click();
  await expect
    .poll(async () =>
      (await api(page, "/provider-config/accounts/")).body.some(
        (a: any) => a.provider === "openai-compatible" && a.is_active,
      ),
    )
    .toBe(false);
  const rejected = await api(
    page,
    `/workspaces/${state.workspace}/harness/sessions/`,
    "POST",
    { prompt: "No account", model: "openai-compatible/local/demo" },
  );
  expect(rejected.status).toBe(400);
  await activeUse(page, `${prefix} A`);
  const started = await api(
    page,
    `/workspaces/${state.workspace}/harness/sessions/`,
    "POST",
    { prompt: "Reply briefly", model: "openai-compatible/local/demo" },
  );
  expect(started.status).toBe(201);
  expect((await settled(page, started.body.id)).finish).toBe("stop");
  await activeUse(page, `${prefix} B`);
  const next = await api(
    page,
    `/harness/sessions/${started.body.id}/message`,
    "POST",
    { prompt: "Reply again" },
  );
  expect(next.status).toBe(202);
  expect((await settled(page, started.body.id)).finish).toBe("stop");
  const blocked = await api(
    page,
    `/workspaces/${state.workspace}/harness/sessions/`,
    "POST",
    {
      prompt: "wait for account removal",
      model: "openai-compatible/local/demo",
    },
  );
  expect(blocked.status).toBe(201);
  await page.getByTestId(`edit-account-${second.id}`).click();
  await expect(
    page.getByLabel("API key (optional)", { exact: true }),
  ).toHaveValue("");
  await page.getByTestId("delete-account").click();
  await page.getByTestId("confirm-account-action").click();
  const aborted = await settled(page, blocked.body.id);
  expect(aborted.finish).toBe("aborted");
  expect(aborted.error).toContain("no longer shared");
  const rows = (await api(page, "/provider-config/accounts/")).body;
  expect(rows.find((a: any) => a.id === first.id).is_active).toBe(false);
  await activeUse(page, `${prefix} A`);
  await page.screenshot({
    path: "/workspace/.opencuria/playwright/provider-accounts-live.png",
  });
  await context.close();
});

test("org admin shares accounts; member can use but neither mutate nor reveal other personal accounts", async ({
  browser,
}) => {
  const admin = await login(browser, "admin");
  const member = await login(browser, "member");
  const label = `Org ${randomUUID().slice(0, 8)}`;
  await admin.page.goto("/?settings=provider");
  await admin.page.getByTestId("provider-manage-openai-compatible").click();
  await admin.page.getByTestId("add-provider-account").click();
  await admin.page.getByLabel("Account name", { exact: true }).fill(label);
  await admin.page
    .getByLabel("Base URL", { exact: true })
    .fill("http://127.0.0.1:8099/v1");
  await admin.page
    .getByLabel("Models (one per line)", { exact: true })
    .fill("local/demo");
  await admin.page.getByTestId("account-shared").click();
  await admin.page.getByTestId("save-provider-account").click();
  const shared = (
    await api(admin.page, "/provider-config/accounts/")
  ).body.find((a: any) => a.label === label);
  expect(shared.scope).toBe("organization");
  const visible = (
    await api(member.page, "/provider-config/accounts/")
  ).body.find((a: any) => a.id === shared.id);
  expect(visible.can_manage).toBe(false);
  expect(
    (
      await api(
        member.page,
        `/provider-config/accounts/openai-compatible/${shared.id}/`,
        "PUT",
        { label: "forbidden" },
      )
    ).status,
  ).toBe(403);
  await activeUse(member.page, label);
  const blocked = await api(
    member.page,
    `/workspaces/${state.workspace}/harness/sessions/`,
    "POST",
    { prompt: "wait for unshare", model: "openai-compatible/local/demo" },
  );
  expect(blocked.status).toBe(201);
  await admin.page.getByTestId(`edit-account-${shared.id}`).click();
  await admin.page.getByTestId("account-shared").click();
  await admin.page.getByTestId("save-provider-account").click();
  await expect(admin.page.getByTestId("account-confirm")).toBeVisible();
  await admin.page.getByTestId("confirm-account-action").click();
  const aborted = await settled(member.page, blocked.body.id);
  expect(aborted.finish).toBe("aborted");
  expect(
    (await api(member.page, "/provider-config/accounts/")).body.some(
      (a: any) => a.id === shared.id,
    ),
  ).toBe(false);
  await admin.page.screenshot({
    path: "/workspace/.opencuria/playwright/provider-accounts-admin.png",
  });
  await admin.context.close();
  await member.context.close();
});
