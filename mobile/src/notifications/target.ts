/** Notification targets are data, never arbitrary URLs or navigation commands. */
export interface NotificationTarget {
  deviceId: string;
  organizationId: string;
  workspaceId: string;
  sessionId: string;
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function notificationTarget(value: unknown): NotificationTarget | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (
    ![row.deviceId, row.organizationId, row.workspaceId, row.sessionId].every(
      (id) => typeof id === "string" && uuid.test(id),
    )
  )
    return null;
  return {
    deviceId: row.deviceId as string,
    organizationId: row.organizationId as string,
    workspaceId: row.workspaceId as string,
    sessionId: row.sessionId as string,
  };
}

export function notificationLink(target: NotificationTarget): string {
  const params = Object.entries(target)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join("&");
  return `opencuria:///?${params}`;
}

export function targetFromLink(url: string): NotificationTarget | null {
  try {
    const parsed = new URL(url);
    if (
      parsed.protocol !== "opencuria:" ||
      parsed.hostname ||
      parsed.pathname !== "/"
    )
      return null;
    return notificationTarget(Object.fromEntries(parsed.searchParams));
  } catch {
    return null;
  }
}
