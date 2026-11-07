import type { PublishedApp } from "@opencuria/client";
import type { InstalledApp } from "./appInstaller";

export type AppPlatform = "android" | "ios";

/**
 * - `available`: published, not on this device
 * - `installed`: on this device, newest version
 * - `update`: on this device, a newer version is published
 * - `incompatible`: built for another Expo SDK than this OpenCuria app runs
 * - `unsupported`: no build for this platform
 */
export type AppState =
  | "available"
  | "installed"
  | "update"
  | "incompatible"
  | "unsupported";

export interface AppRow {
  appId: string;
  name: string;
  state: AppState;
  installed: InstalledApp | null;
  published: PublishedApp | null;
}

/** Major Expo SDK of a version such as "57.0.0". */
export function sdkMajor(version: string | null | undefined): number | null {
  const major = Number.parseInt(String(version ?? ""), 10);
  return Number.isFinite(major) ? major : null;
}

/** The server, organization and user an install belongs to. */
export function appScope(
  serverUrl: string,
  organizationId: string | null | undefined,
  userId: string | number | null | undefined,
): string {
  return `${serverUrl}|${organizationId ?? ""}|${userId ?? ""}`;
}

function publishedState(
  app: PublishedApp,
  platform: AppPlatform,
  hostSdk: number | null,
): AppState | null {
  const latest = app.latest_version;
  if (!latest || !(platform in latest.platforms)) return "unsupported";
  const sdk = sdkMajor(latest.sdk_version);
  if (hostSdk !== null && sdk !== null && sdk !== hostSdk)
    return "incompatible";
  return null;
}

/**
 * Join the server's apps with the ones installed on this device. Installed
 * apps stay listed offline or after the server deleted them; apps installed
 * under another account or server are left out.
 */
export function appRows(
  published: PublishedApp[],
  installed: InstalledApp[],
  options: { scope: string; platform: AppPlatform; hostSdk: number | null },
): AppRow[] {
  const local = new Map(
    installed
      .filter((app) => app.scope === options.scope)
      .map((app) => [app.appId, app]),
  );
  const rows: AppRow[] = published.map((app) => {
    const copy = local.get(app.id) ?? null;
    local.delete(app.id);
    const blocked = publishedState(app, options.platform, options.hostSdk);
    const latest = app.latest_version;
    const state: AppState = copy
      ? latest && latest.number > copy.versionNumber && !blocked
        ? "update"
        : "installed"
      : (blocked ?? "available");
    return {
      appId: app.id,
      name: copy?.name ?? app.name,
      state,
      installed: copy,
      published: app,
    };
  });
  for (const copy of local.values())
    rows.push({
      appId: copy.appId,
      name: copy.name,
      state: "installed",
      installed: copy,
      published: null,
    });
  return rows;
}

/** Installed apps first (by name), then apps to install. */
export function appSections(rows: AppRow[]): {
  installed: AppRow[];
  available: AppRow[];
} {
  const byName = (a: AppRow, b: AppRow) => a.name.localeCompare(b.name);
  return {
    installed: rows.filter((row) => row.installed).sort(byName),
    available: rows.filter((row) => !row.installed).sort(byName),
  };
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** One line under the app's name: version and what can happen next. */
export function appSubtitle(row: AppRow, platform: AppPlatform): string {
  const latest = row.published?.latest_version ?? null;
  const version = (name: string | undefined) => (name ? `Version ${name}` : "");
  switch (row.state) {
    case "update":
      return `Version ${row.installed?.versionName ?? "?"} → ${latest?.version_name ?? "new"}`;
    case "installed":
      return version(row.installed?.versionName) || "Installed";
    case "incompatible":
      return `Needs Expo SDK ${sdkMajor(latest?.sdk_version) ?? "?"}; update OpenCuria`;
    case "unsupported":
      return platform === "ios" ? "Not built for iPhone" : "Not built for Android";
    case "available": {
      const size = latest?.platforms[platform];
      return [version(latest?.version_name), size ? formatBytes(size) : ""]
        .filter(Boolean)
        .join(" · ");
    }
  }
}

/** Number of apps that wait for the user: new ones and updates. */
export function pendingAppCount(rows: AppRow[]): number {
  return rows.filter((row) => row.state === "available" || row.state === "update")
    .length;
}
