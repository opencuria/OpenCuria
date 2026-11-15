import type { InstalledApp } from "../apps/appInstaller";
import { APPS_LINK, appLink } from "../apps/appLinks";

/** One app tile of the home screen widget. */
export interface AppsWidgetApp {
  id: string;
  name: string;
  /** Opens the app in OpenCuria. */
  url: string;
  /**
   * `file://` URL of a small copy of the app's icon; missing without one.
   * The snapshot holds no `null`: iOS keeps it in user defaults, which
   * reject it.
   */
  icon?: string;
  /** Installed or updated and not opened since (iOS marks it like the home screen). */
  isNew: boolean;
  /** Position among the tiles a widget shows: alphabetical, so tiles stay put. */
  order: number;
}

/** What the widget shows: written by the app, drawn by the system. */
export interface AppsWidgetSnapshot {
  /** `signedOut` invites to sign in; `ready` lists the apps (maybe none). */
  status: "ready" | "signedOut";
  /**
   * Most recently used first. A widget shows as many as fit; when some are
   * left over, its last tile opens the Apps list and previews them.
   */
  apps: AppsWidgetApp[];
  /** Opens the Apps list in OpenCuria. */
  appsUrl: string;
}

/** More than the largest widget (iPad, 8 × 4) shows, previews included. */
export const MAX_WIDGET_APPS = 40;

/** Last install, update or open: what "recently used" means here. */
export function lastUsed(app: InstalledApp): string {
  const opened = app.openedAt ?? "";
  const updated = app.updatedAt ?? app.installedAt;
  return opened > updated ? opened : updated;
}

/** The apps of `scope` most recently used first, as many as a widget needs. */
export function widgetApps(
  installed: InstalledApp[],
  scope: string,
): InstalledApp[] {
  return installed
    .filter((app) => app.scope === scope)
    .sort(
      (a, b) =>
        lastUsed(b).localeCompare(lastUsed(a)) ||
        a.appId.localeCompare(b.appId),
    )
    .slice(0, MAX_WIDGET_APPS);
}

/**
 * The widget's content for an account: `scope` null means signed out.
 * `icons` maps app ids to their widget icon (see `prepareWidgetIcons`).
 */
export function appsWidgetSnapshot(
  installed: InstalledApp[],
  scope: string | null,
  icons: Readonly<Record<string, string>> = {},
): AppsWidgetSnapshot {
  if (scope === null)
    return { status: "signedOut", apps: [], appsUrl: APPS_LINK };
  const recent = widgetApps(installed, scope);
  const order = new Map(
    [...recent]
      .sort(
        (a, b) =>
          a.name.localeCompare(b.name) || a.appId.localeCompare(b.appId),
      )
      .map((app, index) => [app.appId, index]),
  );
  return {
    status: "ready",
    appsUrl: APPS_LINK,
    apps: recent.map((app) => ({
      id: app.appId,
      name: app.name,
      url: appLink(app.appId),
      ...(icons[app.appId] ? { icon: icons[app.appId] } : {}),
      isNew: !app.openedAt || app.openedAt < (app.updatedAt ?? app.installedAt),
      order: order.get(app.appId) ?? 0,
    })),
  };
}
