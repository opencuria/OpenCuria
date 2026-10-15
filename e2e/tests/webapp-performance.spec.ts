import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";

const BASE_URL = process.env.E2E_PERF_BASE_URL || "http://127.0.0.1:4174";
const DEFAULT_LABEL =
  new URL(BASE_URL).port === "4174" ? "optimized" : "baseline";
const LABEL = process.env.E2E_PERF_LABEL || DEFAULT_LABEL;
const OUTPUT = "/workspace/.opencuria/playwright/performance/results";
const ORG_ID = "perf-org";
const WORKSPACE_ID = "perf-workspace";
const MESSAGE_COUNT = 20;
const MEMORY_SESSION_COUNT = 12;
const encoder = new TextEncoder();
mkdirSync(OUTPUT, { recursive: true });

test.use({
  baseURL: BASE_URL,
  launchOptions: {
    executablePath:
      process.env.E2E_CHROME_PATH || "/usr/bin/google-chrome-stable",
  },
  viewport: { width: 1440, height: 960 },
  video: "off",
});

test("production load, virtualized streaming, idle CPU and transcript-cache benchmark", async ({
  browser,
}) => {
  const stamp = `${LABEL}-${Date.now()}`;
  const report: Record<string, unknown> = {
    label: LABEL,
    baseUrl: BASE_URL,
    timestamp: new Date().toISOString(),
    browser: "Chromium via /usr/bin/google-chrome-stable",
    fixtures:
      "REST route fixtures + Socket.IO Engine.IO websocket fixture; no provider/runner",
    measurementNotes:
      "Fetched JS is the sum of response body bytes. CDP heap/task/script/long-task metrics are diagnostics only; no percent speedup is asserted. Stream wall time includes bounded DOM sampling and is not treated as an isolated rendering benchmark.",
  };
  const pageErrors: string[] = [];
  const requests: string[] = [];
  let socketEmitter: SocketEmitter | null = null;
  const timelines = new Map<
    string,
    { session: PerfSession; messages: PerfMessage[] }
  >();
  const sessions = makeSessions();
  const conversations = sessions.map((session, index) => ({
    session_id: session.id,
    workspace_id: WORKSPACE_ID,
    workspace_name: "Benchmark workspace",
    title: session.title,
    status: session.status,
    mode: session.mode,
    agent_name: session.agent_name,
    model: "",
    unread: false,
    manual_unread: false,
    needs_attention: false,
    attention_kind: "",
    updated_at: "2026-10-01T12:00:00Z",
    last_message_at: new Date(
      Date.UTC(2026, 9, 14, 12, 0, 0) - index * 60_000,
    ).toISOString(),
  }));
  for (const session of sessions) {
    timelines.set(session.id, {
      session,
      messages: makeTranscript(
        session.id,
        session.id === "long-history" ? 80 : MESSAGE_COUNT,
      ),
    });
  }

  const installPageDiagnostics = async (page: Page) => {
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("requestfailed", (request) =>
      requests.push(
        `${request.method()} ${request.url()}: ${request.failure()?.errorText ?? "unknown"}`,
      ),
    );
    await page.addInitScript(() => {
      const longTasks: number[] = [];
      Object.defineProperty(window, "__perfLongTasks", { value: longTasks });
      try {
        new PerformanceObserver((entries) => {
          for (const entry of entries.getEntries())
            longTasks.push(entry.duration);
        }).observe({ type: "longtask", buffered: true });
      } catch {
        // Long Tasks API is optional; CDP task/script metrics remain available.
      }
    });
  };

  const installSocketFixture = async (page: Page) => {
    await page.routeWebSocket(/\/ws\/runner/, (socket: WebSocketRoute) => {
      socket.send(
        "0" +
          JSON.stringify({
            sid: "perf-engine",
            upgrades: [],
            pingInterval: 100_000,
            pingTimeout: 100_000,
          }),
      );
      socket.onMessage((message) => {
        const frame = String(message);
        if (frame.startsWith("40/frontend")) {
          socket.send(
            "40/frontend," + JSON.stringify({ sid: "perf-frontend" }),
          );
          socketEmitter = (event, data) =>
            socket.send("42/frontend," + JSON.stringify([event, data]));
        } else if (frame === "2") {
          socket.send("3");
        }
      });
    });
  };

  const installRestFixtures = async (page: Page) => {
    await page.route("**/api/v1/**", async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname.replace(/^\/api\/v1/, "");
      const method = route.request().method();
      const json = (data: unknown, status = 200) =>
        route.fulfill({ status, json: data });
      const sessionId = path.match(/^\/harness\/sessions\/([^/]+)/)?.[1];

      if (path === "/auth/providers/")
        return json({
          password_enabled: true,
          sso: {
            enabled: false,
            provider: null,
            authorization_endpoint: null,
            client_id: null,
            scope: null,
            supports_pkce: false,
          },
        });
      if (path === "/auth/me/") return json(perfUser);
      if (path === "/workspaces/") return json([perfWorkspace]);
      if (path === `/workspaces/${WORKSPACE_ID}/`) return json(perfWorkspace);
      if (path === "/harness/conversations/") return json(conversations);
      if (path === `/workspaces/${WORKSPACE_ID}/harness/sessions/`)
        return json(
          sessions.map((session) => ({
            ...session,
            parent_id: null,
            skill_ids: [],
          })),
        );
      if (sessionId && path.endsWith("/timeline")) {
        const transcript = timelines.get(sessionId);
        if (transcript)
          return json({
            session: { ...transcript.session },
            messages: transcript.messages,
          });
        return json({
          session: makeSession(sessionId, "Unknown chat"),
          messages: [],
        });
      }
      if (sessionId && path.endsWith("/todos")) return json([]);
      if (
        sessionId &&
        path.includes("/messages/") &&
        path.endsWith("/edit") &&
        method === "POST"
      ) {
        const messageId = path.split("/messages/")[1]?.split("/")[0];
        const transcript = timelines.get(sessionId);
        const edited = route.request().postDataJSON() as { prompt?: string };
        const message = transcript?.messages.find(
          (row) => row.id === messageId,
        );
        if (message && edited.prompt) message.content = edited.prompt;
        return json({
          ...(transcript?.session ?? makeSession(sessionId, "Edited chat")),
          status: "idle",
        });
      }
      if (sessionId && /\/(read|unread)\/?$/.test(path))
        return route.fulfill({ status: 204 });
      if (
        path === `/workspaces/${WORKSPACE_ID}/desktop/status/` ||
        path.includes("/desktop/status/")
      )
        return json({
          active: false,
          proxy_url: null,
          viewer_held: false,
          computer_use_active: false,
        });
      if (path.endsWith("/provider-config/models/")) return json([]);
      if (path.endsWith("/agent-configs/") || path.endsWith("/skills/"))
        return json([]);
      if (path.endsWith("/harness/engines/")) return json([]);
      if (path.endsWith("/provider-config/")) return json({});
      if (path.endsWith("/agent-s-config/"))
        return json({
          grounding_model: "",
          grounding_width: 1920,
          grounding_height: 1080,
          model_temperature: null,
          max_steps: 20,
          max_trajectory_length: 30,
          enable_reflection: false,
          enable_code_agent: false,
          enable_recording: false,
          screenshot_max_dimension: 1280,
          action_pre_delay: 0,
          action_post_delay: 0,
          wait_delay: 0,
        });
      if (path.endsWith("/recent-models/")) return json([]);
      if (path === `/workspaces/${WORKSPACE_ID}/terminal/`) {
        await route.fulfill({
          status: 200,
          json: { task_id: "perf-terminal-start" },
        });
        await waitForSocket(() => socketEmitter);
        socketEmitter!("terminal:started", {
          workspace_id: WORKSPACE_ID,
          terminal_id: "perf-terminal",
          task_id: "perf-terminal-start",
        });
        return;
      }
      if (path.includes("/files/"))
        return json({ entries: [], path: "/workspace" });
      // Keep unrelated production UI queries deterministic without reaching a real backend.
      return json([]);
    });
  };

  const preparePage = async (page: Page, auth: boolean) => {
    await installPageDiagnostics(page);
    if (auth) {
      const payload = Buffer.from(JSON.stringify({ exp: 4102444800 })).toString(
        "base64url",
      );
      await page.addInitScript((token) => {
        localStorage.setItem("kern_access_token", token);
        localStorage.setItem("kern_refresh_token", "performance-refresh");
        localStorage.setItem("kern_active_org_id", "perf-org");
        localStorage.setItem(
          "kern_user",
          JSON.stringify({
            id: 17,
            email: "perf@example.test",
            first_name: "Perf",
            last_name: "Tester",
          }),
        );
        localStorage.setItem(
          "kern_organizations",
          JSON.stringify([
            { id: "perf-org", name: "Perf org", slug: "perf", role: "admin" },
          ]),
        );
      }, `fixture.${payload}.signature`);
    }
    await installSocketFixture(page);
    await installRestFixtures(page);
  };

  const captureScripts = (page: Page) => {
    const bodies: Array<Promise<{ url: string; bytes: number } | null>> = [];
    page.on("response", (response) => {
      if (
        response.request().resourceType() !== "script" ||
        response.status() < 200 ||
        response.status() >= 300
      )
        return;
      bodies.push(
        response
          .body()
          .then((body) => ({
            url: new URL(response.url()).pathname,
            bytes: body.byteLength,
          }))
          .catch(() => null),
      );
    });
    return async () =>
      (await Promise.all(bodies)).filter(
        (item): item is { url: string; bytes: number } => item !== null,
      );
  };

  const loginContext = await browser.newContext({
    viewport: { width: 1440, height: 960 },
  });
  const loginPage = await loginContext.newPage();
  await preparePage(loginPage, false);
  const loginScripts = captureScripts(loginPage);
  await loginPage.goto("/");
  await expect(
    loginPage.getByRole("button", { name: "Sign in" }),
  ).toBeVisible();
  await expect(loginPage.getByLabel("Email")).toBeVisible();
  await loginPage.screenshot({ path: `${OUTPUT}/${stamp}-login.png` });
  const loginScriptBodies = await loginScripts();
  report.loginUnauthenticated = {
    jsResponseCount: loginScriptBodies.length,
    fetchedJsBytes: loginScriptBodies.reduce(
      (sum, item) => sum + item.bytes,
      0,
    ),
    scripts: loginScriptBodies,
    url: loginPage.url(),
  };
  await loginContext.close();

  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
  });
  const page = await context.newPage();
  await preparePage(page, true);
  const homeScripts = captureScripts(page);
  const cdp = await context.newCDPSession(page);
  await cdp.send("Performance.enable");
  await cdp.send("HeapProfiler.enable");
  await page.goto("/");
  await expect(page.getByTestId("chat-home")).toBeVisible();
  await expect(page.getByTestId("chat-home-greeting")).toContainText("perf");
  await expect(page.getByTestId("workspace-conversation-group")).toBeVisible();
  await waitForSocket(() => socketEmitter);
  const emitFrontend: SocketEmitter = (event, data) => {
    const currentEmitter = socketEmitter;
    if (!currentEmitter)
      throw new Error("Socket.IO fixture disconnected during benchmark");
    currentEmitter(event, data);
  };
  const initialHomeScripts = await homeScripts();
  const terminalInitialUrls = initialHomeScripts.filter((item) =>
    /WorkspaceTerminal|terminal/i.test(item.url),
  );
  report.homeAuthenticated = {
    jsResponseCount: initialHomeScripts.length,
    fetchedJsBytes: initialHomeScripts.reduce(
      (sum, item) => sum + item.bytes,
      0,
    ),
    scripts: initialHomeScripts,
    terminalRelatedScriptsAtHome: terminalInitialUrls,
  };
  await page.screenshot({ path: `${OUTPUT}/${stamp}-home.png` });

  // The settings query is a real production deep link, exercised on the built app.
  await page.goto("/?settings=agents");
  await expect(page.getByTestId("settings-sheet")).toBeVisible();
  await expect(page.getByTestId("settings-sheet-title")).toHaveText(/Agents/i);
  await expect(
    page.getByRole("tab", { name: "Agents" }).first(),
  ).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("settings-sheet")).toBeHidden();

  const idleBefore = await readCdpMetrics(cdp);
  await page.waitForTimeout(1_000);
  const idleAfter = await readCdpMetrics(cdp);
  await collectGarbage(cdp);
  const homeHeap = await readCdpMetrics(cdp);
  report.homeRuntime = {
    idleMeasurementMs: 1_000,
    idleTaskDurationDeltaMs: delta(
      idleBefore.TaskDuration,
      idleAfter.TaskDuration,
    ),
    idleScriptDurationDeltaMs: delta(
      idleBefore.ScriptDuration,
      idleAfter.ScriptDuration,
    ),
    heapAfterGcBytes: homeHeap.JSHeapUsedSize,
    longTasksMs: await page.evaluate(
      () =>
        (window as Window & { __perfLongTasks?: number[] }).__perfLongTasks ??
        [],
    ),
  };

  // Open the long transcript through the production sidebar row (not a store/router injection).
  const historyRow = page.getByRole("button", {
    name: "Open chat Long history",
    exact: true,
  });
  await expect(historyRow).toBeVisible();
  await historyRow.click();
  await expect(page).toHaveURL(/session=long-history/);
  await expect(
    page.locator('[data-message-id="long-history-m79"]'),
  ).toBeVisible();
  await page.waitForTimeout(150);
  const historyDomCount = await page.locator("[data-message-id]").count();
  expect(
    historyDomCount,
    "virtualized 80-message history must keep a bounded message DOM",
  ).toBeLessThanOrEqual(40);
  expect(historyDomCount).toBeGreaterThan(0);
  await page.screenshot({ path: `${OUTPUT}/${stamp}-virtualized-history.png` });

  const chatIdleBefore = await readCdpMetrics(cdp);
  await page.waitForTimeout(1_000);
  const chatIdleAfter = await readCdpMetrics(cdp);
  await collectGarbage(cdp);
  const chatIdleHeap = await readCdpMetrics(cdp);
  report.chatRuntime = {
    idleMeasurementMs: 1_000,
    idleTaskDurationDeltaMs: delta(
      chatIdleBefore.TaskDuration,
      chatIdleAfter.TaskDuration,
    ),
    idleScriptDurationDeltaMs: delta(
      chatIdleBefore.ScriptDuration,
      chatIdleAfter.ScriptDuration,
    ),
    heapAfterGcBytes: chatIdleHeap.JSHeapUsedSize,
    historyMessageDomNodes: historyDomCount,
  };

  const streamMarkdown = makeStreamMarkdown();
  expect(encoder.encode(streamMarkdown).byteLength).toBeGreaterThanOrEqual(
    20_000,
  );
  await page.evaluate(() =>
    (window as Window & { __perfLongTasks?: number[] }).__perfLongTasks?.splice(
      0,
    ),
  );
  const streamStart = await readCdpMetrics(cdp);
  const streamWallStart = Date.now();
  const streamedMessageId = "stream-answer";
  const streamedPartId = "stream-answer-text";
  emitFrontend("harness.session_status", {
    workspace_id: WORKSPACE_ID,
    session_id: "long-history",
    status: "busy",
    message_id: streamedMessageId,
  });
  // Establish that the busy frame reached the compiled app before pacing the deltas.
  await expect(
    page.locator(`[data-message-id="${streamedMessageId}"]`),
  ).toBeVisible();
  const chunks = Array.from({ length: 200 }, (_, index) =>
    streamMarkdown.slice(
      Math.floor((index * streamMarkdown.length) / 200),
      Math.floor(((index + 1) * streamMarkdown.length) / 200),
    ),
  );
  const sampledDomCounts: Array<{ afterUpdate: number; messageNodes: number }> =
    [
      {
        afterUpdate: 0,
        messageNodes: await page.locator("[data-message-id]").count(),
      },
    ];
  for (let index = 0; index < chunks.length; index += 1) {
    emitFrontend("harness.part_updated", {
      workspace_id: WORKSPACE_ID,
      session_id: "long-history",
      message_id: streamedMessageId,
      part_id: streamedPartId,
      part_position: 0,
      delta: { text: chunks[index] },
    });
    if ((index + 1) % 50 === 0) {
      // Let the browser consume the prior batch, once per 50 events rather than per delta.
      await page.waitForTimeout(5);
      await expect(
        page.locator(`[data-message-id="${streamedMessageId}"]`),
      ).toBeVisible();
      sampledDomCounts.push({
        afterUpdate: index + 1,
        messageNodes: await page.locator("[data-message-id]").count(),
      });
    }
    // Pace fixture emissions from Node; do not round-trip to the browser per delta.
    await sleep(5);
  }
  await expect(
    page.locator(`[data-message-id="${streamedMessageId}"]`),
  ).toContainText("STREAM-END-MARKER", { timeout: 15_000 });
  const streamWallDurationMs = Date.now() - streamWallStart;
  const streamEnd = await readCdpMetrics(cdp);
  const streamLongTasks = await page.evaluate(
    () =>
      (window as Window & { __perfLongTasks?: number[] }).__perfLongTasks ?? [],
  );
  sampledDomCounts.push({
    afterUpdate: chunks.length,
    messageNodes: await page.locator("[data-message-id]").count(),
  });
  await page.screenshot({ path: `${OUTPUT}/${stamp}-streamed-answer.png` });
  report.streaming = {
    historyMessages: 80,
    socketUpdates: chunks.length,
    payloadUtf8Bytes: encoder.encode(streamMarkdown).byteLength,
    requestedPacingMs: 5,
    wallDurationMs: streamWallDurationMs,
    cdpTaskDurationDeltaMs: delta(
      streamStart.TaskDuration,
      streamEnd.TaskDuration,
    ),
    cdpScriptDurationDeltaMs: delta(
      streamStart.ScriptDuration,
      streamEnd.ScriptDuration,
    ),
    cdpScriptDurationPerSocketUpdateMs:
      delta(streamStart.ScriptDuration, streamEnd.ScriptDuration) === null
        ? null
        : Math.round(
            ((delta(streamStart.ScriptDuration, streamEnd.ScriptDuration) ??
              0) *
              1_000) /
              chunks.length,
          ) / 1_000,
    meanWallMsPerSocketUpdate:
      Math.round((streamWallDurationMs / chunks.length) * 100) / 100,
    longTasksMs: streamLongTasks,
    sampledMessageDomCounts: sampledDomCounts,
    maxSampledMessageDomNodesDuringStreaming: Math.max(
      ...sampledDomCounts.map((sample) => sample.messageNodes),
    ),
  };
  expect(await page.locator("[data-message-id]").count()).toBeLessThanOrEqual(
    40,
  );

  // The terminal is a large, lazy production feature: not fetched on Home or chat,
  // but downloaded once the user opens its real workspace side-panel tab.
  await expect(
    page.getByTestId("workspace-chat-header-toggle-side-panel"),
  ).toBeVisible();
  await page.getByTestId("workspace-chat-header-toggle-side-panel").click();
  await expect(page.getByTestId("workspace-side-panel")).toBeVisible();
  await page.getByTestId("side-panel-tab-terminal").click();
  await expect(page.getByTestId("side-panel-tab-terminal"))
    .toHaveAttribute("data-state", "active", { timeout: 15_000 })
    .catch(async () => {
      await expect(
        page.locator(
          '[data-testid="side-panel-tab-terminal"][data-state="active"]',
        ),
      ).toBeVisible();
    });
  await expect(page.getByTestId("workspace-terminal")).toBeVisible();
  await page.waitForTimeout(250);
  const terminalRequests = await page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .map((entry) => entry.name)
      .filter((url) => /WorkspaceTerminal|terminal/i.test(url)),
  );
  const initialTerminalRequests = initialHomeScripts.filter((item) =>
    /WorkspaceTerminal|terminal/i.test(item.url),
  );
  report.terminalLazyLoad = {
    initialHomeTerminalJs: initialTerminalRequests,
    terminalResourcesAfterActivation: terminalRequests,
  };
  if (isOptimizedBuild()) {
    expect(
      initialTerminalRequests,
      "optimized Home must not download the terminal feature",
    ).toEqual([]);
    expect(
      terminalRequests,
      "terminal chunk must load after selecting the terminal tab",
    ).not.toEqual([]);
  }

  // Browse all 12 real sidebar rows. Each timeline is a ~64 KiB deterministic
  // transcript; measure retained JS heap after V8 GC at each SPA selection.
  const heapSamples: Array<{ sessionId: string; bytes: number }> = [];
  await page.getByTestId("workspace-chat-header-toggle-side-panel").click();
  const rows = page.getByTestId("conversation-row");
  while ((await rows.count()) < sessions.length) {
    const more = page.getByTestId("show-more-chats").first();
    await expect(more).toBeVisible();
    await more.click();
  }
  expect(await rows.count()).toBe(sessions.length);
  for (let index = 0; index < MEMORY_SESSION_COUNT; index += 1) {
    const sessionId = `memory-${String(index).padStart(2, "0")}`;
    const row = page.getByRole("button", {
      name: `Open chat Memory chat ${String(index + 1).padStart(2, "0")}`,
      exact: true,
    });
    await expect(row).toBeVisible();
    await row.click();
    await expect(page).toHaveURL(new RegExp(`session=${sessionId}`));
    await expect(
      page.locator(`[data-message-id="${sessionId}-m19"]`),
    ).toBeVisible();
    await collectGarbage(cdp);
    const memory = await readCdpMetrics(cdp);
    heapSamples.push({ sessionId, bytes: memory.JSHeapUsedSize });
    if (index === 5)
      await page.screenshot({ path: `${OUTPUT}/${stamp}-memory-midpoint.png` });
    if (index === MEMORY_SESSION_COUNT - 1)
      await page.screenshot({ path: `${OUTPUT}/${stamp}-memory-12-chats.png` });
  }

  // First visited transcript remains readable after eviction pressure; edit via
  // the rendered production message action and verify the REST-backed change.
  const firstMemoryRow = page.getByRole("button", {
    name: "Open chat Memory chat 01",
    exact: true,
  });
  await firstMemoryRow.click();
  await expect(page).toHaveURL(/session=memory-00/);
  const firstUser = page.locator('[data-message-id="memory-00-m00"]');
  await expect(firstUser).toContainText("session=memory-00 message 0");
  await firstUser.getByTestId("message-edit").click();
  const editedText =
    "Retained transcript edit verified through the compiled production UI.";
  await page.getByTestId("message-edit-input").fill(editedText);
  await page.getByTestId("message-edit-save").click();
  await expect(firstUser).toContainText(editedText);

  // Realtime busy → idle status updates continue to update the actual sidebar/chat state.
  emitFrontend("harness.session_status", {
    workspace_id: WORKSPACE_ID,
    session_id: "memory-00",
    status: "busy",
  });
  await expect(firstMemoryRow.getByTestId("busy-spinner")).toBeVisible();
  // Move away before idle so the normal background-unread flow is exercised without
  // issuing a mark-read request for the conversation currently being viewed.
  await page
    .getByRole("button", { name: "Open chat Memory chat 02", exact: true })
    .click();
  await expect(page).toHaveURL(/session=memory-01/);
  emitFrontend("harness.session_status", {
    workspace_id: WORKSPACE_ID,
    session_id: "memory-00",
    status: "idle",
  });
  await expect(firstMemoryRow.getByTestId("busy-spinner")).toHaveCount(0);

  await collectGarbage(cdp);
  const finalHeap = await readCdpMetrics(cdp);
  const finalRows = await page.locator("[data-message-id]").count();
  report.transcriptCache = {
    visitedSessions: MEMORY_SESSION_COUNT,
    messagesPerMemorySession: MESSAGE_COUNT,
    approximateUtf8TranscriptBytesPerSession: 64 * 1024,
    heapAfterGcBytesBySession: heapSamples,
    heapAfterGcBytesAfterReopenAndEdit: finalHeap.JSHeapUsedSize,
    heapGrowthFromFirstToTwelfthBytes:
      heapSamples.at(-1)!.bytes - heapSamples[0]!.bytes,
    finalMessageDomNodes: finalRows,
    firstSessionReopened: true,
    productionMessageEditRoundTrip: true,
    realtimeBusyIdleStatus: true,
  };
  expect(finalRows).toBeLessThanOrEqual(40);
  expect(
    pageErrors,
    "compiled app should not produce uncaught page errors",
  ).toEqual([]);
  expect(
    requests,
    "fixtures should satisfy every production REST request",
  ).toEqual([]);

  report.pageErrors = pageErrors;
  report.failedRequests = requests;
  report.assertions = {
    settingsAgentsDeepLink: "passed",
    virtualizedHistoryDomBounded: historyDomCount <= 40,
    streamedAnswerEndMarker: "passed",
    allTwelveChatsVisitedBySidebar: heapSamples.length === MEMORY_SESSION_COUNT,
    firstSessionReopenEditAndSocketStatus: "passed",
    terminalLazyLoadAssertionsApplied: isOptimizedBuild(),
    pageErrors: "none",
  };
  writeFileSync(`${OUTPUT}/${stamp}.json`, JSON.stringify(report, null, 2));
  await cdp.detach();
  await context.close();
});

interface PerfSession {
  id: string;
  workspace_id: string;
  title: string;
  mode: "build";
  agent_name: "build";
  model: string;
  reasoning_effort: string;
  status: "idle" | "busy";
  cost: number;
  tokens: Record<string, number>;
  harness_id: "native";
  updated_at: string;
  created_at: string;
}

interface PerfMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  parts: Array<Record<string, unknown>>;
  position: number;
  message_position: number;
  created_at: string;
  completed_at: string | null;
  cost: number;
  tokens: Record<string, number>;
}

function makeSession(id: string, title: string): PerfSession {
  return {
    id,
    workspace_id: WORKSPACE_ID,
    title,
    mode: "build",
    agent_name: "build",
    model: "",
    reasoning_effort: "",
    status: "idle",
    cost: 0,
    tokens: {},
    harness_id: "native",
    updated_at: "2026-10-14T12:00:00Z",
    created_at: "2026-10-14T11:00:00Z",
  };
}

function makeSessions(): PerfSession[] {
  return [
    makeSession("long-history", "Long history"),
    ...Array.from({ length: MEMORY_SESSION_COUNT }, (_, index) =>
      makeSession(
        `memory-${String(index).padStart(2, "0")}`,
        `Memory chat ${String(index + 1).padStart(2, "0")}`,
      ),
    ),
  ];
}

function deterministicText(
  seed: number,
  length: number,
  prefix: string,
): string {
  let state = seed >>> 0 || 1;
  const alphabet =
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-.,;:!?/<>[]{}()";
  let value = prefix;
  while (value.length < length) {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    value += alphabet[(state >>> 0) % alphabet.length];
  }
  return value.slice(0, length);
}

function makeTranscript(sessionId: string, count: number): PerfMessage[] {
  return Array.from({ length: count }, (_, index) => {
    const role = index % 2 === 0 ? "user" : "assistant";
    const content = deterministicText(
      [...sessionId].reduce(
        (sum, char) => sum + char.charCodeAt(0),
        index + 17,
      ),
      sessionId === "long-history" ? 1_024 : 3_277,
      `session=${sessionId} message ${index}; deterministic transcript — `,
    );
    const id = `${sessionId}-m${String(index).padStart(2, "0")}`;
    return {
      id,
      session_id: sessionId,
      role,
      content,
      parts:
        role === "assistant"
          ? [
              {
                id: `${id}-text`,
                session_id: sessionId,
                message_id: id,
                type: "text",
                state: "completed",
                title: "",
                output: content,
                position: 0,
                detail_loaded: true,
              },
            ]
          : [],
      position: index,
      message_position: index,
      created_at: new Date(Date.UTC(2026, 9, 14, 11, 0, index)).toISOString(),
      completed_at:
        role === "assistant"
          ? new Date(Date.UTC(2026, 9, 14, 11, 0, index + 1)).toISOString()
          : null,
      cost: 0,
      tokens: {},
    };
  });
}

function makeStreamMarkdown(): string {
  const lines = [
    "# Production benchmark response",
    "",
    "Streaming this deterministic Markdown through Socket.IO in 200 updates.",
    "",
  ];
  for (let index = 0; index < 260; index += 1) {
    lines.push(
      `- Update ${String(index).padStart(3, "0")}: **stable rendered text** with [a link](https://example.test/${index}) and inline \`code\`.`,
    );
  }
  lines.push("", "STREAM-END-MARKER");
  return lines.join("\n");
}

const perfWorkspace = {
  id: WORKSPACE_ID,
  name: "Benchmark workspace",
  status: "running",
  runner_online: true,
  runner_id: "perf-runner",
  active_operation: null,
  has_active_session: false,
  runtime_type: "docker",
  credential_ids: [],
  plugin_ids: [],
  credentials_present: false,
  created_by_id: 17,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-14T12:00:00Z",
  last_activity_at: "2026-10-14T12:00:00Z",
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
};

const perfUser = {
  id: 17,
  email: "perf@example.test",
  first_name: "Perf",
  last_name: "Tester",
  organizations: [
    { id: ORG_ID, name: "Performance fixtures", slug: "perf", role: "admin" },
  ],
};

type SocketEmitter = (event: string, data: unknown) => void;

async function waitForSocket(
  getEmitter: () => SocketEmitter | null,
): Promise<SocketEmitter> {
  await expect.poll(() => getEmitter() !== null).toBe(true);
  const emitter = getEmitter();
  if (!emitter) throw new Error("Socket.IO fixture did not connect");
  return emitter;
}

async function readCdpMetrics(
  cdp: Awaited<
    ReturnType<import("@playwright/test").BrowserContext["newCDPSession"]>
  >,
) {
  const result = await cdp.send("Performance.getMetrics");
  return Object.fromEntries(
    result.metrics.map((metric) => [metric.name, metric.value]),
  ) as Record<string, number>;
}

async function collectGarbage(
  cdp: Awaited<
    ReturnType<import("@playwright/test").BrowserContext["newCDPSession"]>
  >,
): Promise<void> {
  await cdp.send("HeapProfiler.collectGarbage");
}

function delta(before?: number, after?: number): number | null {
  if (before === undefined || after === undefined) return null;
  // CDP Performance metrics are seconds; report durations in milliseconds.
  return Math.round((after - before) * 1_000_000) / 1_000;
}

function isOptimizedBuild(): boolean {
  return LABEL.toLowerCase() === "optimized";
}

declare global {
  interface Window {
    __perfLongTasks?: number[];
  }
}
