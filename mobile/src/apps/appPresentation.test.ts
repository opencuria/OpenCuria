import { describe, expect, it } from "vitest";
import type { PublishedApp } from "@opencuria/client";
import type { InstalledApp } from "./appInstaller";
import {
  appRows,
  appScope,
  appSections,
  appSubtitle,
  formatBytes,
  pendingAppCount,
  sdkMajor,
} from "./appPresentation";

const SCOPE = appScope("https://curia", "org", 1);

function published(
  id: string,
  name: string,
  version: Partial<NonNullable<PublishedApp["latest_version"]>> = {},
): PublishedApp {
  return {
    id,
    name,
    slug: name.toLowerCase(),
    workspace_id: "ws",
    workspace_name: "dev",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    latest_version: {
      id: `${id}-v`,
      number: 2,
      version_name: "1.1.0",
      sdk_version: "57.0.0",
      notes: "",
      created_at: "2026-11-01T00:00:00Z",
      icon_sha256: null,
      platforms: { android: 2_500_000, ios: 2_400_000 },
      ...version,
    },
  };
}

function installed(appId: string, number: number, scope = SCOPE): InstalledApp {
  return {
    appId,
    scope,
    name: appId,
    slug: appId,
    versionId: `${appId}-old`,
    versionNumber: number,
    versionName: `1.${number - 1}.0`,
    sdkVersion: "57.0.0",
    publishedAt: "2026-10-01T00:00:00Z",
    installedAt: "2026-10-02T00:00:00Z",
    hasIcon: false,
  };
}

const options = { scope: SCOPE, platform: "android" as const, hostSdk: 57 };

describe("appRows", () => {
  it("joins published and installed apps into states", () => {
    const rows = appRows(
      [
        published("new", "New"),
        published("current", "Current"),
        published("stale", "Stale"),
        published("future", "Future", { sdk_version: "58.0.0" }),
        published("ios-only", "Phone", { platforms: { ios: 10 } }),
      ],
      [
        installed("current", 2),
        installed("stale", 1),
        installed("deleted", 1),
        installed("other-account", 1, appScope("https://curia", "org", 2)),
      ],
      options,
    );
    const state = Object.fromEntries(rows.map((row) => [row.appId, row.state]));
    expect(state).toEqual({
      new: "available",
      current: "installed",
      stale: "update",
      future: "incompatible",
      "ios-only": "unsupported",
      deleted: "installed",
    });
    expect(pendingAppCount(rows)).toBe(2);
    const sections = appSections(rows);
    expect(sections.installed.map((row) => row.appId)).toEqual([
      "current",
      "deleted",
      "stale",
    ]);
    expect(sections.available.map((row) => row.appId)).toEqual([
      "future",
      "new",
      "ios-only",
    ]);
  });

  it("never offers an update the device cannot run", () => {
    const [row] = appRows(
      [published("app", "App", { sdk_version: "58.0.0", number: 5 })],
      [installed("app", 1)],
      options,
    );
    expect(row?.state).toBe("installed");
  });
});

describe("appSubtitle", () => {
  it("describes the version and the next step", () => {
    const rows = appRows(
      [published("a", "A"), published("b", "B", { sdk_version: "58.0.0" })],
      [installed("c", 1)],
      options,
    );
    expect(rows.map((row) => appSubtitle(row, "android"))).toEqual([
      "Version 1.1.0 · 2.4 MB",
      "Needs Expo SDK 58; update OpenCuria",
      "Version 1.0.0",
    ]);
    const update = appRows([published("c", "C")], [installed("c", 1)], options);
    expect(appSubtitle(update[0]!, "android")).toBe(
      "Version 1.0.0 → 1.1.0",
    );
  });

  it("formats sizes and SDK versions", () => {
    expect(formatBytes(800)).toBe("1 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
    expect(sdkMajor("57.0.0")).toBe(57);
    expect(sdkMajor(undefined)).toBeNull();
  });
});
