/**
 * Links that open an installed app, or the Apps list, from outside OpenCuria
 * (the home screen widget). Like notification links they carry data, never
 * navigation commands, and keep the root path so Expo Router stays on `/`.
 */
export type AppLink = { kind: "app"; appId: string } | { kind: "apps" };

/** Server-issued app ids, as the installer accepts them. */
const APP_ID = /^[A-Za-z0-9_-]{1,64}$/;

export const APPS_LINK = "opencuria:///?apps";

export function appLink(appId: string): string {
  return `opencuria:///?app=${encodeURIComponent(appId)}`;
}

export function parseAppLink(url: string): AppLink | null {
  try {
    const parsed = new URL(url);
    if (
      parsed.protocol !== "opencuria:" ||
      parsed.hostname ||
      parsed.pathname !== "/"
    )
      return null;
    const appId = parsed.searchParams.get("app");
    if (appId !== null)
      return APP_ID.test(appId) ? { kind: "app", appId } : null;
    return parsed.searchParams.has("apps") ? { kind: "apps" } : null;
  } catch {
    return null;
  }
}
