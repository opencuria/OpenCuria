/** Apps agents send to the user's phone with the `app_publish` tool. */

import type { HarnessPart } from "./harness";

/** The app version a successful `app_publish` call published. */
export interface PublishedAppReference {
  appId: string;
  versionId: string;
  name: string;
  /** Sequential version number, starting at 1. */
  version: number | null;
  /** Expo config `version`, e.g. "1.2.0". */
  versionName: string;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * The published app of a finished `app_publish` tool part, read from the
 * server's bounded `display.app` projection; null for any other part.
 */
export function publishedAppReference(
  part: HarnessPart,
): PublishedAppReference | null {
  if (part.type !== "tool" || part.state !== "completed") return null;
  const app = part.display?.app;
  if (!app || typeof app !== "object") return null;
  const row = app as Record<string, unknown>;
  const appId = text(row.app_id);
  const versionId = text(row.version_id);
  if (!appId || !versionId) return null;
  return {
    appId,
    versionId,
    name: text(row.name) || "App",
    version: typeof row.version === "number" ? row.version : null,
    versionName: text(row.version_name),
  };
}

/**
 * The apps a message sent to the phone, shown below the message rather than
 * where the agent called the tool: each app once, with the newest version it
 * sent, in the order the apps were first sent.
 */
export function messagePublishedApps(
  parts: readonly HarnessPart[],
): PublishedAppReference[] {
  const apps = new Map<string, PublishedAppReference>();
  const ordered = [...parts].sort(
    (left, right) => (left.position ?? 0) - (right.position ?? 0),
  );
  for (const part of ordered) {
    const app = publishedAppReference(part);
    // Replacing a key keeps its place, so a later version updates the card.
    if (app) apps.set(app.appId, app);
  }
  return [...apps.values()];
}
