import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
// @ts-expect-error -- plain Node script without type declarations
import { computeAppRuntime, serializeAppRuntime } from "../../scripts/sync-app-runtime.mjs";

const RUNTIME = new URL(
  "../../../backend/apps/mobile_apps/runtime.json",
  import.meta.url,
);

describe("app runtime spec", () => {
  it("matches the native modules this app ships", () => {
    // Run `node mobile/scripts/sync-app-runtime.mjs` after dependency changes.
    expect(readFileSync(RUNTIME, "utf8")).toBe(
      serializeAppRuntime(computeAppRuntime()),
    );
  });
});
