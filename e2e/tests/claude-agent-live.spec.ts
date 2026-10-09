import { mkdirSync } from "node:fs";
import { expect, test } from "@playwright/test";

const EMAIL = "claude-live@localhost.test";
const PASSWORD = "OpenCuria-claude-live-2026!";
const WORKSPACE = "e2e-claude-live-workspace";
const QUESTION = "Should the local fixture finish this run?";
const ANSWER =
  "The pinned Claude Agent SDK and native CLI completed this run against the isolated local API fixture.";
const SYNTHETIC_API_TOKEN = "sk-ant-live-fixture-only-not-a-secret";
const SYNTHETIC_SUBSCRIPTION_TOKEN = "sk-ant-subscription-fixture-only";
const READ_RESULT =
  "The synthetic README heading is Synthetic Claude LIVE workspace.";
const AUTH_STORAGE_KEYS = [
  "kern_access_token",
  "kern_refresh_token",
  "kern_user",
  "kern_organizations",
  "kern_active_org_id",
];

type TimelinePart = {
  type?: string;
  state?: string;
  call_id?: string;
  title?: string;
  output?: string;
  display?: Record<string, unknown>;
};

type TimelineMessage = {
  role: string;
  content: string;
  parts: TimelinePart[];
  tokens?: Record<string, number>;
};

type PersistedHarnessSession = {
  id: string;
  status: string;
  tokens?: Record<string, number>;
};

async function persistedSession(
  page: import("@playwright/test").Page,
  workspaceId: string,
  sessionId: string,
): Promise<PersistedHarnessSession | undefined> {
  return page.evaluate(
    async ({ workspaceId: id, sessionId: targetSessionId }) => {
      const response = await fetch(
        `/api/v1/workspaces/${id}/harness/sessions/`,
        {
          headers: {
            Authorization: `Bearer ${localStorage.getItem("kern_access_token") ?? ""}`,
            "X-Organization-Id":
              localStorage.getItem("kern_active_org_id") ?? "",
          },
        },
      );
      if (!response.ok)
        throw new Error(`Session list request failed: ${response.status}`);
      const sessions = (await response.json()) as PersistedHarnessSession[];
      return sessions.find((session) => session.id === targetSessionId);
    },
    { workspaceId, sessionId },
  );
}

async function persistedMessages(
  page: import("@playwright/test").Page,
  sessionId: string,
): Promise<TimelineMessage[]> {
  return page.evaluate(
    async ({ id }) => {
      const token = localStorage.getItem("kern_access_token") ?? "";
      const organizationId = localStorage.getItem("kern_active_org_id") ?? "";
      const response = await fetch(`/api/v1/harness/sessions/${id}/timeline`, {
        headers: {
          Authorization: `Bearer ${token}`,
          "X-Organization-Id": organizationId,
        },
      });
      if (!response.ok)
        throw new Error(`Timeline request failed: ${response.status}`);
      return (await response.json()).messages as TimelineMessage[];
    },
    { id: sessionId },
  );
}

async function persistedSubtasks(
  page: import("@playwright/test").Page,
  sessionId: string,
) {
  const messages = await persistedMessages(page, sessionId);
  return messages
    .flatMap((message) => message.parts ?? [])
    .filter((part) => part.type === "subtask");
}

async function persistedTaskArguments(
  page: import("@playwright/test").Page,
  sessionId: string,
): Promise<Record<string, unknown>[]> {
  return page.evaluate(
    async ({ id }) => {
      const response = await fetch(`/api/v1/harness/sessions/${id}/parts`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("kern_access_token") ?? ""}`,
          "X-Organization-Id": localStorage.getItem("kern_active_org_id") ?? "",
        },
      });
      if (!response.ok)
        throw new Error(`Parts request failed: ${response.status}`);
      const parts = (await response.json()).messages.flatMap(
        (message: {
          parts?: Array<{
            type?: string;
            state?: string;
            input?: Record<string, unknown>;
          }>;
        }) => message.parts ?? [],
      );
      return parts
        .filter(
          (part: {
            type?: string;
            state?: string;
            input?: Record<string, unknown>;
          }) =>
            part.type === "tool" &&
            part.state === "completed" &&
            part.input?.tool === "task",
        )
        .map((part: { input?: Record<string, unknown> }) => {
          const args = part.input?.arguments;
          if (typeof args !== "string" || !args.trim()) return null;
          return JSON.parse(args) as Record<string, unknown>;
        })
        .filter(
          (
            args: Record<string, unknown> | null,
          ): args is Record<string, unknown> => args !== null,
        );
    },
    { id: sessionId },
  );
}

async function persistedAssistantText(
  page: import("@playwright/test").Page,
  sessionId: string,
): Promise<string> {
  const messages = await persistedMessages(page, sessionId);
  return messages
    .filter((message) => message.role === "assistant")
    .map(
      (message) =>
        `${message.content}\n${message.parts.map((part) => part.output ?? "").join("\n")}`,
    )
    .join("\n");
}

/** End-to-end through the real Vue UI, Django REST/WS, SDK and native CLI. */
test("Claude Agent LIVE: native CLI, real tools, question gate and final answer", async ({
  page,
}) => {
  test.skip(
    process.env.E2E_CLAUDE_AGENT_LIVE !== "1",
    "Opt in with E2E_CLAUDE_AGENT_LIVE=1",
  );
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  // A clean origin storage ensures this run selects its freshly seeded org.
  await page.goto("/");
  await page.evaluate(
    (keys) => keys.forEach((key) => localStorage.removeItem(key)),
    AUTH_STORAGE_KEYS,
  );
  await page.reload();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByTestId("chat-home-greeting")).toContainText(
    "claude-live",
  );

  // Exercise both personal authentication modes through the actual settings UI
  // and REST endpoint. Fixture-only tokens never authorize remote inference.
  await page.goto("/?settings=provider");
  await expect(page.getByTestId("settings-sheet")).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Provider & Models" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("provider-status-claude-agent")).toHaveText(
    "Connected",
  );
  await page.getByTestId("provider-manage-claude-agent").click();
  await expect(page.getByTestId("connection-status")).toContainText(
    "API token",
  );
  await expect(page.getByTestId("claude-token")).toHaveValue("");

  await page.getByTestId("claude-tab-subscription-token").click();
  await expect(page.getByLabel("Anthropic subscription token")).toBeVisible();
  await page.getByTestId("claude-token").fill(SYNTHETIC_SUBSCRIPTION_TOKEN);
  const subscriptionSave = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/harness/engines/claude/connection/") &&
      response.request().method() === "PUT",
  );
  await page.getByTestId("save-claude-agent").click();
  const subscriptionResponse = await subscriptionSave;
  expect(subscriptionResponse.ok()).toBeTruthy();
  const subscriptionPayload = (await subscriptionResponse.json()) as Record<
    string,
    unknown
  >;
  expect(subscriptionPayload).toMatchObject({
    auth_type: "subscription_token",
    connected: true,
  });
  expect(JSON.stringify(subscriptionPayload)).not.toContain(
    SYNTHETIC_SUBSCRIPTION_TOKEN,
  );
  await expect(page.getByTestId("provider-detail-claude-agent")).toContainText(
    "Subscription token",
  );

  await page.getByTestId("provider-manage-claude-agent").click();
  await expect(page.getByTestId("connection-status")).toContainText(
    "Subscription token",
  );
  await expect(page.getByTestId("claude-token")).toHaveValue("");
  await page.getByTestId("claude-tab-api-token").click();
  await page.getByTestId("claude-token").fill(SYNTHETIC_API_TOKEN);
  const apiTokenSave = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/harness/engines/claude/connection/") &&
      response.request().method() === "PUT",
  );
  await page.getByTestId("save-claude-agent").click();
  const apiTokenResponse = await apiTokenSave;
  expect(apiTokenResponse.ok()).toBeTruthy();
  const apiTokenPayload = (await apiTokenResponse.json()) as Record<
    string,
    unknown
  >;
  expect(apiTokenPayload).toMatchObject({
    auth_type: "api_token",
    connected: true,
  });
  expect(JSON.stringify(apiTokenPayload)).not.toContain(SYNTHETIC_API_TOKEN);
  await expect(page.getByTestId("provider-detail-claude-agent")).toContainText(
    "API token",
  );
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("settings-sheet")).toBeHidden();

  await expect(page.getByTestId("workspace-picker-trigger")).toContainText(
    WORKSPACE,
  );
  await page.getByTestId("workspace-row").click();
  await expect(page).toHaveURL(/\/workspaces\/[0-9a-f-]+$/i);
  const workspaceId = new URL(page.url()).pathname.split("/").pop()!;
  await expect(
    page.getByTestId("workspace-chat-header-chat-title"),
  ).toContainText("New chat");

  const composer = page.getByTestId("composer-textarea");
  await composer.fill("Start the local Claude Agent live E2E run.");
  await expect(page.getByTestId("composer-send")).toBeEnabled();
  await page.getByTestId("composer-mode-trigger").click();
  await page.getByTestId("composer-mode-claude-plan").click();
  await expect(page.getByTestId("composer-mode-trigger")).toContainText(
    "Claude Agent Plan",
  );
  await page.getByTestId("composer-mode-trigger").click();
  await page.getByTestId("composer-mode-claude-build").click();
  await expect(page.getByTestId("composer-mode-trigger")).toContainText(
    "Claude Agent Build",
  );
  await composer.fill(
    "Delegate a read-only README inspection, then ask me before finishing the local E2E.",
  );
  await page.getByTestId("composer-send").click();

  await expect(page).toHaveURL(
    /\/workspaces\/[0-9a-f-]+\?session=[0-9a-f-]+/i,
    { timeout: 20_000 },
  );
  const rootSessionId = new URL(page.url()).searchParams.get("session");
  expect(rootSessionId).toBeTruthy();
  const subtaskRow = page.getByTestId("harness-subtask-row").first();
  await expect(subtaskRow).toBeVisible({ timeout: 75_000 });
  await expect(subtaskRow).toContainText(
    "Inspect the synthetic workspace README",
  );
  await expect(page.getByTestId("composer-question-sheet")).toBeVisible({
    timeout: 75_000,
  });
  await expect(page.getByTestId("composer-question-sheet")).toContainText(
    QUESTION,
  );

  // The root asked a real SDK child Agent to Read the isolated README.
  // Confirm the child turn and Read output from REST persistence before answering.
  const task = (await persistedSubtasks(page, rootSessionId!)).find(
    (part) => part.title === "Inspect the synthetic workspace README",
  )!;
  const childSessionId = String(task.display?.["child_session_id"] ?? "");
  expect(childSessionId).toMatch(/^[0-9a-f-]{36}$/i);
  await expect
    .poll(() => persistedAssistantText(page, childSessionId), {
      timeout: 30_000,
    })
    .toContain(READ_RESULT);
  const taskCalls = await persistedTaskArguments(page, rootSessionId!);
  expect(taskCalls).toContainEqual(
    expect.objectContaining({
      description: "Inspect the synthetic workspace README",
      prompt: "Read /workspace/README.md and return its heading.",
      subagent_type: "explore",
    }),
  );

  await page.getByRole("radio", { name: /Continue/ }).click();
  await page.getByTestId("composer-question-submit").click();

  await expect(
    page.locator('[data-testid="harness-chat-dropzone"]'),
  ).toContainText(ANSWER, { timeout: 45_000 });
  await expect(page.getByTestId("composer-stop")).toHaveCount(0);
  await expect
    .poll(
      async () => {
        const [session, messages] = await Promise.all([
          persistedSession(page, workspaceId, rootSessionId!),
          persistedMessages(page, rootSessionId!),
        ]);
        const assistantTokens = messages
          .filter((message) => message.role === "assistant")
          .reduce((total, message) => total + (message.tokens?.total ?? 0), 0);
        return (
          session?.status === "idle" &&
          (session.tokens?.total ?? 0) > 0 &&
          assistantTokens > 0
        );
      },
      { timeout: 45_000 },
    )
    .toBe(true);
  const persistedRootSession = await persistedSession(
    page,
    workspaceId,
    rootSessionId!,
  );
  const finalRootMessages = await persistedMessages(page, rootSessionId!);
  const finalAssistantTokens = finalRootMessages
    .filter((message) => message.role === "assistant")
    .reduce((total, message) => total + (message.tokens?.total ?? 0), 0);
  expect(persistedRootSession?.tokens?.total).toBeGreaterThan(0);
  expect(finalAssistantTokens).toBeGreaterThan(0);
  expect(finalAssistantTokens).toBeLessThanOrEqual(
    persistedRootSession?.tokens?.total ?? 0,
  );
  expect(pageErrors).toEqual([]);

  await expect(page.getByTestId("harness-worked-for")).toBeVisible();
  await page
    .getByTestId("harness-worked-for")
    .locator('[data-slot="collapsible-trigger"]')
    .first()
    .click();
  await expect(page.getByTestId("harness-question-card")).toContainText(
    "Continue",
  );
  expect(pageErrors).toEqual([]);

  // Reload from the session URL: answer/question/subagent must load back
  // from persisted backend state, independently of the streaming socket.
  await page.reload();
  await expect(
    page.locator('[data-testid="harness-chat-dropzone"]'),
  ).toContainText(ANSWER, { timeout: 30_000 });
  await expect(page.getByTestId("composer-mode-trigger")).toContainText(
    "Claude Agent Build",
  );
  const workedFor = page.getByTestId("harness-worked-for");
  if (await workedFor.count()) {
    await workedFor
      .locator('[data-slot="collapsible-trigger"]')
      .first()
      .click();
  }
  await expect(page.getByTestId("harness-question-card")).toContainText(
    "Continue",
  );
  await expect(
    page.getByTestId("harness-question-card-answer").first(),
  ).toHaveText("Continue");
  await expect(page.getByTestId("composer-stop")).toHaveCount(0);
  expect(pageErrors).toEqual([]);

  mkdirSync("/workspace/.opencuria/playwright", { recursive: true });
  await page.screenshot({
    path: "/workspace/.opencuria/playwright/claude-agent-live-final.png",
    fullPage: true,
  });

  // Open the SDK-created child session and verify its persisted native Read result.
  await page.getByTestId("harness-subtask-row").first().click();
  await expect(
    page.getByTestId("workspace-chat-header-back-to-parent"),
  ).toBeVisible();
  await expect(page.getByTestId("harness-chat-dropzone")).toContainText(
    READ_RESULT,
    {
      timeout: 30_000,
    },
  );
});
