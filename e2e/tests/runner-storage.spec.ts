import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect } from "@playwright/test";
import { installRunnerBackend } from "../fixtures/runner-storage.fixture";

const GiB = 1024 ** 3;
const artifacts = resolve(__dirname, "../..", ".opencuria/playwright");

test("real runner storage chart, merged map, inspection and refresh", async ({
  page,
}, testInfo) => {
  const backend = await installRunnerBackend(page);
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
