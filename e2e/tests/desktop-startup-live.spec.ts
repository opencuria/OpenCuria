import { inflateSync } from "node:zlib";
import { mkdirSync, readFileSync } from "node:fs";
import { test, expect, type Page } from "@playwright/test";

const BASE_URL = process.env.E2E_BASE_URL || "http://127.0.0.1:5173";
const WORKSPACE_ID = process.env.E2E_DESKTOP_STARTUP_WORKSPACE_ID || "";
const ARTIFACT_DIR = "/workspace/.opencuria/playwright";
const UUID_PATTERN = /^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/i;

type DesktopIntent = {
  viewer_client_id?: string;
  intent_revision?: number;
};

type LiveTestWindow = Window & {
  __desktopStartupIframe?: HTMLIFrameElement;
  __desktopStartupWindow?: Window | null;
};

type EncodedPng = {
  width: number;
  height: number;
  bitDepth: number;
  colorType: number;
  interlace: number;
  data: Buffer;
};

const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const PNG_BYTES_PER_PIXEL: Record<number, number> = {
  0: 1,
  2: 3,
  3: 1,
  4: 2,
  6: 4,
};

function readPngPixels(contents: Buffer): EncodedPng {
  if (!contents.subarray(0, 8).equals(PNG_SIGNATURE)) {
    throw new Error("The live desktop screenshot is not a PNG image.");
  }
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlace = 0;
  const compressed: Buffer[] = [];
  for (let offset = 8; offset < contents.length; ) {
    const length = contents.readUInt32BE(offset);
    const type = contents.toString("ascii", offset + 4, offset + 8);
    const payload = contents.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      width = payload.readUInt32BE(0);
      height = payload.readUInt32BE(4);
      bitDepth = payload[8]!;
      colorType = payload[9]!;
      interlace = payload[12]!;
    } else if (type === "IDAT") {
      compressed.push(payload);
    } else if (type === "IEND") {
      break;
    }
    offset += length + 12;
  }
  if (!width || !height || bitDepth !== 8 || interlace !== 0) {
    throw new Error("The live screenshot has an unsupported PNG pixel format.");
  }
  return {
    width,
    height,
    bitDepth,
    colorType,
    interlace,
    data: inflateSync(Buffer.concat(compressed)),
  };
}

function countRenderedCanvasPixels(png: EncodedPng): number {
  const bytesPerPixel = PNG_BYTES_PER_PIXEL[png.colorType];
  if (!bytesPerPixel) throw new Error("Unsupported screenshot color type.");
  const stride = png.width * bytesPerPixel + 1;
  let rendered = 0;
  let prior = Buffer.alloc(stride - 1);
  for (let y = 0; y < png.height; y += 1) {
    const filter = png.data[y * stride]!;
    const scanline = Buffer.from(
      png.data.subarray(y * stride + 1, (y + 1) * stride),
    );
    for (let i = 0; i < scanline.length; i += 1) {
      const left = i >= bytesPerPixel ? scanline[i - bytesPerPixel]! : 0;
      const up = prior[i]!;
      const upperLeft = i >= bytesPerPixel ? prior[i - bytesPerPixel]! : 0;
      if (filter === 1) scanline[i] = (scanline[i]! + left) & 0xff;
      else if (filter === 2) scanline[i] = (scanline[i]! + up) & 0xff;
      else if (filter === 3)
        scanline[i] = (scanline[i]! + Math.floor((left + up) / 2)) & 0xff;
      else if (filter === 4) {
        const p = left + up - upperLeft;
        const dl = Math.abs(p - left);
        const du = Math.abs(p - up);
        const dul = Math.abs(p - upperLeft);
        scanline[i] =
          (scanline[i]! +
            (dl <= du && dl <= dul ? left : du <= dul ? up : upperLeft)) &
          0xff;
      } else if (filter !== 0)
        throw new Error("Unsupported PNG scanline filter.");
    }
    // A real Kasm canvas in this viewer is an opaque RGB/RGBA image, unlike
    // the cleared all-transparent canvas immediately after WebSocket handshake.
    if (bytesPerPixel === 4 || bytesPerPixel === 2) {
      for (let i = bytesPerPixel - 1; i < scanline.length; i += bytesPerPixel) {
        if (scanline[i] !== 0) rendered += 1;
      }
    } else {
      for (let i = 0; i < scanline.length; i += bytesPerPixel) {
        if (scanline[i] !== 0 || scanline[i + 1] !== 0 || scanline[i + 2] !== 0)
          rendered += 1;
      }
    }
    prior = scanline;
  }
  return rendered;
}

let activeWorkspaceId: string | null = null;
const viewerIntents = new Map<string, number>();

async function releaseOwnViewerLeases(page: Page): Promise<void> {
  if (!activeWorkspaceId || viewerIntents.size === 0 || page.isClosed()) return;

  const workspaceId = activeWorkspaceId;
  const intents = [...viewerIntents].map(
    ([viewer_client_id, intent_revision]) => ({
      viewer_client_id,
      intent_revision,
    }),
  );

  // Navigate away first to unmount only this page's desktop owners. Then send
  // the explicit, revision-fenced tombstone for every browser incarnation. A
  // hard reload creates a new viewer UUID; this never stops the desktop VM or
  // touches a viewer lease from another browser.
  await page.goto(`${BASE_URL}/workspaces`, { waitUntil: "domcontentloaded" });
  const statuses = await page.evaluate(
    async ({ workspaceId, intents }) => {
      const access = localStorage.getItem("kern_access_token");
      const organization = localStorage.getItem("kern_active_org_id");
      if (!access) return [0];
      return Promise.all(
        intents.map(async (intent) => {
          const response = await fetch(
            `/api/v1/workspaces/${workspaceId}/desktop/stop/`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${access}`,
                "X-Organization-Id": organization ?? "",
              },
              body: JSON.stringify(intent),
            },
          );
          return response.status;
        }),
      );
    },
    { workspaceId, intents },
  );
  for (const status of statuses) expect([200, 202, 409]).toContain(status);
  activeWorkspaceId = null;
  viewerIntents.clear();
}

test.describe("opt-in live desktop startup and recovery", () => {
  test.skip(
    process.env.E2E_DESKTOP_STARTUP_LIVE !== "1",
    "Set E2E_DESKTOP_STARTUP_LIVE=1 to run against a disposable live workspace.",
  );
  test.skip(
    !UUID_PATTERN.test(WORKSPACE_ID),
    "Set E2E_DESKTOP_STARTUP_WORKSPACE_ID to a running workspace UUID.",
  );

  test.setTimeout(180_000);
  test.afterEach(async ({ page }) => {
    await releaseOwnViewerLeases(page);
  });

  test("recovers startup, preserves the VNC viewer through UI switches, and reconnects after RFB drop", async ({
    page,
  }) => {
    mkdirSync(ARTIFACT_DIR, { recursive: true });
    activeWorkspaceId = null;
    viewerIntents.clear();

    let statusGets = 0;
    let desktopStarts = 0;
    const desktopStartIntents = new Set<string>();
    let desktopWebSockets = 0;
    let mainBundleRequests = 0;
    let delayedMainBundleMs = 0;
    let mainBundleWasDelayed = false;
    let mainBundleResponseCount = 0;
    const startsByViewer = new Map<string, number>();
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (
        url.pathname.includes(`/ws/desktop/${WORKSPACE_ID}/`) &&
        url.pathname.endsWith("/dist/main.bundle.js")
      ) {
        mainBundleResponseCount += 1;
      }
    });

    // Delay only the first actual Kasm main bundle, then continue through to
    // the real runner response. This tests a slow viewer bootstrap without
    // mocking or replacing any Kasm assets.
    await page.route("**/dist/main.bundle.js", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (!pathname.includes(`/ws/desktop/${WORKSPACE_ID}/`)) {
        await route.continue();
        return;
      }
      mainBundleRequests += 1;
      if (!mainBundleWasDelayed) {
        mainBundleWasDelayed = true;
        const startedAt = Date.now();
        await new Promise((resolve) => setTimeout(resolve, 2_000));
        delayedMainBundleMs = Date.now() - startedAt;
      }
      await route.continue();
    });

    await page.route(
      `**/api/v1/workspaces/${WORKSPACE_ID}/desktop/status/**`,
      async (route) => {
        statusGets += 1;
        if (statusGets === 1) {
          // Exercise the real coordinator's retry after a transient API outage.
          await route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Temporary live E2E outage" }),
          });
          return;
        }
        await route.continue();
      },
    );

    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname === `/api/v1/workspaces/${WORKSPACE_ID}/desktop/`
      ) {
        desktopStarts += 1;
        try {
          const intent = request.postDataJSON() as DesktopIntent;
          if (
            intent.viewer_client_id &&
            UUID_PATTERN.test(intent.viewer_client_id) &&
            typeof intent.intent_revision === "number"
          ) {
            activeWorkspaceId = WORKSPACE_ID;
            const revision = intent.intent_revision;
            if (
              !viewerIntents.has(intent.viewer_client_id) ||
              intent.intent_revision >
                viewerIntents.get(intent.viewer_client_id)!
            ) {
              desktopStartIntents.add(intent.viewer_client_id);
            }
            startsByViewer.set(
              intent.viewer_client_id,
              (startsByViewer.get(intent.viewer_client_id) ?? 0) + 1,
            );
            viewerIntents.set(intent.viewer_client_id, revision);
          }
        } catch {
          // Assertions below report a missing or malformed live viewer intent.
        }
      }
    });

    page.on("websocket", (socket) => {
      if (new URL(socket.url()).pathname === `/ws/desktop/${WORKSPACE_ID}/`) {
        desktopWebSockets += 1;
      }
    });

    // Start with deterministic per-test local UI state, without changing other
    // browser contexts, fixture files, or any persisted workspace data.
    await page.addInitScript(() => {
      localStorage.removeItem("opencuria-side-panel-state");
    });

    // Authenticate through the real login UI, using the same admin fixture
    // defaults as fixtures/auth.fixture.ts. Credentials and tokens are never logged.
    await page.goto(`${BASE_URL}/login`);
    await page
      .getByLabel("Email")
      .fill(process.env.E2E_ADMIN_EMAIL || "admin@localhost.test");
    await page
      .getByLabel("Password")
      .fill(process.env.E2E_ADMIN_PASSWORD || "OpenCuria2026!");
    await page.getByRole("button", { name: /sign in/i }).click();
    await page.waitForURL((url) => !url.pathname.includes("/login"), {
      timeout: 20_000,
    });

    await page.goto(`${BASE_URL}/workspaces/${WORKSPACE_ID}`);
    await expect(page.getByTestId("workspace-chat-header")).toBeVisible();
    await page.getByTestId("workspace-chat-header-toggle-side-panel").click();
    await page.getByTestId("side-panel-tab-desktop").click();

    const iframe = page.locator('[data-testid="desktop-surface-iframe"]');
    await expect
      .poll(() => statusGets, { timeout: 12_000 })
      .toBeGreaterThanOrEqual(2);
    await expect.poll(() => desktopStarts, { timeout: 12_000 }).toBe(1);
    await expect.poll(() => viewerIntents.size, { timeout: 5_000 }).toBe(1);

    // Observe the actual same-origin Kasm/noVNC viewer, not a mocked iframe.
    const expectVncConnected = async (): Promise<void> => {
      await expect
        .poll(
          () =>
            iframe.evaluate((element) =>
              Boolean(
                (
                  element as HTMLIFrameElement
                ).contentDocument?.documentElement.classList.contains(
                  "noVNC_connected",
                ),
              ),
            ),
          { timeout: 45_000, intervals: [100, 250, 500, 1_000] },
        )
        .toBe(true);
      await expect(page.getByTestId("desktop-surface-loading")).toHaveCount(0);
      await expect(iframe).toHaveCount(1);
    };
    await expectVncConnected();
    await expect
      .poll(() => mainBundleRequests, { timeout: 10_000 })
      .toBeGreaterThan(0);
    expect(mainBundleWasDelayed).toBe(true);
    expect(delayedMainBundleMs).toBeGreaterThanOrEqual(1_900);

    await expect
      .poll(() => mainBundleResponseCount, { timeout: 5_000 })
      .toBe(1);

    // Keep the exact iframe element and browsing context as identity checks
    // while only the visible host changes between sidebar and modal.
    const saveViewerIdentity = async (): Promise<void> => {
      await page.evaluate(() => {
        const frame = document.querySelector<HTMLIFrameElement>(
          '[data-testid="desktop-surface-iframe"]',
        );
        const diagnostic = window as LiveTestWindow;
        diagnostic.__desktopStartupIframe = frame ?? undefined;
        diagnostic.__desktopStartupWindow = frame?.contentWindow;
      });
    };
    await saveViewerIdentity();

    const expectSameViewer = async (): Promise<void> => {
      await expect
        .poll(() =>
          page.evaluate(() => {
            const diagnostic = window as LiveTestWindow;
            const frame = document.querySelector<HTMLIFrameElement>(
              '[data-testid="desktop-surface-iframe"]',
            );
            return {
              sameElement: frame === diagnostic.__desktopStartupIframe,
              sameWindow:
                frame?.contentWindow === diagnostic.__desktopStartupWindow,
              frameCount: document.querySelectorAll(
                '[data-testid="desktop-surface-iframe"]',
              ).length,
            };
          }),
        )
        .toEqual({ sameElement: true, sameWindow: true, frameCount: 1 });
    };

    await page.getByTestId("side-panel-desktop-maximize").click();
    await expect(page.getByTestId("workspace-desktop-modal")).toBeVisible();
    await expectSameViewer();
    await page.getByTestId("desktop-modal-close").click();
    await expect(page.getByTestId("workspace-desktop-modal")).toHaveCount(0);
    await expectSameViewer();

    await page.getByTestId("side-panel-tab-git").click();
    await page.getByTestId("side-panel-tab-desktop").click();
    await expectSameViewer();
    expect(desktopStarts).toBe(1);

    // A hard reload creates a fresh page + viewer UUID. Repeat three real
    // workspace/sidebar startup cycles and verify each uses only one start.
    for (let cycle = 1; cycle <= 3; cycle += 1) {
      const knownViewers = new Set(viewerIntents.keys());
      const startsBeforeReload = desktopStarts;
      const bundleResponsesBeforeReload = mainBundleResponseCount;
      await page.reload({ waitUntil: "domcontentloaded", timeout: 20_000 });
      await expect(page.getByTestId("workspace-chat-header")).toBeVisible();
      const panelToggle = page.getByTestId(
        "workspace-chat-header-toggle-side-panel",
      );
      await expect(panelToggle).toHaveAttribute("title", "Open side panel");
      await panelToggle.click();
      await page.getByTestId("side-panel-tab-desktop").click();
      await expectVncConnected();
      await expect
        .poll(() => desktopStartIntents.size)
        .toBeGreaterThan(startsBeforeReload);
      expect(desktopStarts - startsBeforeReload).toBe(1);
      const newestViewer = [...desktopStartIntents].find(
        (clientId) => !knownViewers.has(clientId),
      );
      expect(newestViewer).toBeTruthy();
      const observedLease = viewerIntents.get(newestViewer!);
      expect(observedLease).toBeGreaterThan(0);
      expect(startsByViewer.get(newestViewer!)).toBe(1);
      // Keep tracking every viewer identity, including those whose lease
      // status remains unknown while another viewer holds the shared display.
      if (!viewerIntents.has(newestViewer!)) {
        viewerIntents.set(newestViewer!, observedLease!);
      }
      const unexpectedViewers = [...desktopStartIntents].filter(
        (clientId) => !viewerIntents.has(clientId),
      );
      expect(unexpectedViewers).toEqual([]);
      await expect
        .poll(() => mainBundleResponseCount, { timeout: 45_000 })
        .toBeGreaterThan(bundleResponsesBeforeReload);
      expect(
        await iframe.evaluate((element) =>
          Boolean(
            (
              element as HTMLIFrameElement
            ).contentDocument?.documentElement.classList.contains(
              "noVNC_connected",
            ),
          ),
        ),
      ).toBe(true);
    }

    // Disconnect through Kasm's real RFB control. The real client should report
    // disconnected, show recovery UI, recreate its iframe, then handshake again.
    await saveViewerIdentity();
    await iframe.evaluate((element) => {
      const disconnect = (
        element as HTMLIFrameElement
      ).contentDocument?.querySelector<HTMLInputElement>(
        "#noVNC_disconnect_button",
      );
      if (!disconnect)
        throw new Error(
          "The connected Kasm viewer has no RFB disconnect control.",
        );
      disconnect.click();
    });
    await expect(page.getByTestId("desktop-surface-loading")).toContainText(
      "Desktop disconnected. Reconnecting…",
      { timeout: 5_000 },
    );

    const startsBeforeRfbRecovery = desktopStarts;
    await expect
      .poll(
        () =>
          iframe.evaluate((element) =>
            Boolean(
              (
                element as HTMLIFrameElement
              ).contentDocument?.documentElement.classList.contains(
                "noVNC_connected",
              ),
            ),
          ),
        { timeout: 20_000, intervals: [100, 250, 500, 1_000] },
      )
      .toBe(true);
    await expect(page.getByTestId("desktop-surface-loading")).toHaveCount(0);
    await expect(iframe).toHaveCount(1);
    expect(
      desktopStarts - startsBeforeRfbRecovery,
      "RFB recovery must reuse the existing desktop session",
    ).toBe(0);
    expect(desktopStarts).toBe(4);
    expect(
      desktopWebSockets,
      "the viewer must establish a fresh real VNC websocket",
    ).toBeGreaterThanOrEqual(5);

    // Maximize once more and capture a sharp, desktop-sized artifact only
    // after a real canvas has nonzero area and nontransparent rendered pixels.
    await page.getByTestId("side-panel-desktop-maximize").click();
    await expect(page.getByTestId("workspace-desktop-modal")).toBeVisible();
    const finalScreenshot = `${ARTIFACT_DIR}/desktop-startup-live.png`;
    let renderedCanvasPixels = 0;
    await expect
      .poll(
        async () => {
          renderedCanvasPixels = countRenderedCanvasPixels(
            readPngPixels(await iframe.screenshot({ animations: "disabled" })),
          );
          return renderedCanvasPixels;
        },
        { timeout: 20_000, intervals: [500, 1_000, 2_000] },
      )
      .toBeGreaterThan(100);
    expect(renderedCanvasPixels).toBeGreaterThan(100);
    await page.screenshot({
      path: finalScreenshot,
      fullPage: false,
      animations: "disabled",
    });
    console.log(
      "[desktop-startup-live]",
      JSON.stringify({
        statusGets,
        desktopStartPosts: desktopStarts,
        reloadCycles: 3,
        rfbRecoveryExtraStartPosts: desktopStarts - startsBeforeRfbRecovery,
        desktopWebSockets,
        delayedMainBundleMs,
        mainBundleRequests,
        mainBundleResponses: mainBundleResponseCount,
        renderedCanvasPixels,
      }),
    );
  });
});
