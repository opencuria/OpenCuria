import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { installRunnerBackend } from "../fixtures/runner-storage.fixture";
import type { RunnerStorage } from "../../webapp/src/types/runnerStorage";

function snapshot(): RunnerStorage {
  if (process.env.RUNNER_ACTIVITY_SNAPSHOT)
    return JSON.parse(
      readFileSync(process.env.RUNNER_ACTIVITY_SNAPSHOT, "utf8"),
    );
  const created_at = new Date().toISOString();
  return {
    runner_id: "storage-runner",
    runner_online: true,
    latest_snapshot_id: null,
    latest_complete: true,
    runtimes: [],
    generations: [],
    capture_requests: [],
    operations: [
      {
        task_id: "blender-stop",
        target: "blender",
        phase: "intervention",
        task__status: "failed",
        task__error: "Unknown outcome; intervention required",
        deliveries: 1,
      },
    ],
    activities: [
      {
        id: "blender-stop",
        kind: "operation",
        title: "Stop workspace",
        target_name: "Blender",
        state: "attention",
        outcome: "blocked",
        summary:
          "Stop workspace could not be confirmed. Check the current status before making further changes.",
        diagnostic: "Unknown outcome; intervention required",
        operation_ids: ["blender-stop"],
        created_at,
      },
      {
        id: "capture",
        kind: "capture",
        title: "Save workspace as image",
        target_name: "OpenCuria 3",
        state: "history",
        outcome: "resolved",
        summary: "Interrupted operation closed. Existing resources were kept.",
        diagnostic: "Interrupted child acknowledged",
        operation_ids: [],
        created_at,
      },
    ],
  };
}

test("runner attention counts workflows and puts handled failures in history", async ({
  page,
}, info) => {
  const storage = snapshot();
  const backend = await installRunnerBackend(page, {
    snapshot: storage,
    runnerName: "Zalman",
  });
  await page.goto("/?settings=runners");
  await page.getByRole("button", { name: "Zalman", exact: true }).click();
  const cockpit = page.getByTestId("runner-cockpit");
  await cockpit
    .getByRole("button", { name: "1 activity needs attention", exact: true })
    .click();
  const attention = cockpit.getByRole("region", {
    name: "Needs attention",
    exact: true,
  });
  await expect(attention.locator("article")).toHaveCount(1);
  await expect(attention).toContainText("Blender");
  await expect(attention).not.toContainText("Deliveries");
  await expect(
    cockpit.getByRole("region", { name: "History", exact: true }),
  ).not.toBeVisible();
  await cockpit.getByRole("button", { name: /^History \(/ }).click();
  const history = cockpit.getByRole("region", { name: "History", exact: true });
  await expect(history).toContainText("OpenCuria 3");
  await expect(history).toContainText("Handled");
  await expect(
    history.getByRole("button", { name: "Check status" }),
  ).toHaveCount(0);
  const hasOverflow = await cockpit.evaluate(
    (element) => element.scrollWidth > element.clientWidth + 1,
  );
  expect(hasOverflow).toBe(false);
  const artifacts = resolve(__dirname, "../..", ".opencuria/playwright");
  mkdirSync(artifacts, { recursive: true });
  await page.screenshot({
    path: resolve(artifacts, `runner-activity-${info.project.name}.png`),
    fullPage: true,
  });
  expect(backend.errors).toEqual([]);
  expect(backend.unexpected).toEqual([]);
});
