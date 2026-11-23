import type { PublishedApp } from "@opencuria/client";
import { describeServerUrl } from "../screens/serverConfig";
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
  /**
   * Server of the other account (server, organization or user) the copy was
   * installed under; null for the signed-in account's apps. Such copies only
   * open and leave the device: their server is not signed in.
   */
  otherAccount: string | null;
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

/** The server of an {@link appScope}, as people know it: "curia.example". */
export function scopeServerLabel(scope: string): string {
  const api = scope.split("|").slice(0, -2).join("|");
  return describeServerUrl(api.replace(/\/api\/v1$/, "")) || "another server";
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
 * under another account or server are listed too, with `otherAccount` set.
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
  const listed = new Set(published.map((app) => app.id));
  // The server listing an app another scope installed (the same server under
  // another URL) keeps the server's row, which installs over that copy.
  const elsewhere = installed.filter(
    (app) => app.scope !== options.scope && !listed.has(app.appId),
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
      otherAccount: null,
    };
  });
  for (const copy of local.values())
    rows.push({
      appId: copy.appId,
      name: copy.name,
      state: "installed",
      installed: copy,
      published: null,
      otherAccount: null,
    });
  for (const copy of elsewhere)
    rows.push({
      appId: copy.appId,
      name: copy.name,
      state: "installed",
      installed: copy,
      published: null,
      otherAccount: scopeServerLabel(copy.scope),
    });
  return rows;
}

/**
 * Installed apps first (by name), then apps to install, then apps installed
 * under other accounts.
 */
export function appSections(rows: AppRow[]): {
  installed: AppRow[];
  available: AppRow[];
  other: AppRow[];
} {
  const byName = (a: AppRow, b: AppRow) => a.name.localeCompare(b.name);
  const own = rows.filter((row) => row.otherAccount === null);
  return {
    installed: own.filter((row) => row.installed).sort(byName),
    available: own.filter((row) => !row.installed).sort(byName),
    other: rows.filter((row) => row.otherAccount !== null).sort(byName),
  };
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Download progress (0–1) of an install, e.g. "Downloading… 42%". */
export function downloadLabel(progress: number): string {
  return `Downloading… ${Math.round(progress * 100)}%`;
}

/**
 * One line under the app's name: version and what can happen next, or the
 * download progress (0–1) while it installs.
 */
export function appSubtitle(
  row: AppRow,
  platform: AppPlatform,
  progress?: number,
): string {
  if (progress !== undefined) return downloadLabel(progress);
  const latest = row.published?.latest_version ?? null;
  const version = (name: string | undefined) => (name ? `Version ${name}` : "");
  switch (row.state) {
    case "update":
      return `Version ${row.installed?.versionName ?? "?"} → ${latest?.version_name ?? "new"}`;
    case "installed":
      // Another account's copy never updates here: where it came from matters.
      if (row.otherAccount !== null) return row.otherAccount;
      return version(row.installed?.versionName) || "Installed";
    case "incompatible":
      return `Needs Expo SDK ${sdkMajor(latest?.sdk_version) ?? "?"}; update OpenCuria`;
    case "unsupported":
      return platform === "ios"
        ? "Not built for iPhone"
        : "Not built for Android";
    case "available": {
      const size = latest?.platforms[platform];
      return [version(latest?.version_name), size ? formatBytes(size) : ""]
        .filter(Boolean)
        .join(" · ");
    }
  }
}

/**
 * The one action an app offers, as in the App Store: install, update (then
 * open) or open. Apps this device cannot run offer none.
 */
export function appActionTitle(
  state: AppState,
): "Install" | "Update" | "Open" | null {
  switch (state) {
    case "available":
      return "Install";
    case "update":
      return "Update";
    case "installed":
      return "Open";
    default:
      return null;
  }
}

/** Number of apps that wait for the user: new ones and updates. */
export function pendingAppCount(rows: AppRow[]): number {
  return rows.filter(
    (row) => row.state === "available" || row.state === "update",
  ).length;
}
