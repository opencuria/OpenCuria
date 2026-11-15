import type { ApiKey, ApiKeyPermission } from "@opencuria/client";
import { wallClockDate, wallClockValue } from "../../../ui/wallClock";
import { count, formatDate } from "../../format";

const DAY_MS = 86_400_000;

/** Permissions grouped as the server lists them, in first-seen group order. */
export function groupPermissions(
  permissions: ApiKeyPermission[],
): Array<{ group: string; permissions: ApiKeyPermission[] }> {
  const groups = new Map<string, ApiKeyPermission[]>();
  for (const permission of permissions) {
    const list = groups.get(permission.group) ?? [];
    list.push(permission);
    groups.set(permission.group, list);
  }
  return [...groups].map(([group, entries]) => ({
    group,
    permissions: entries,
  }));
}

/**
 * Whether a key has every permission. Creating or saving a key with no
 * permissions grants all that exist at that moment, and the server then
 * lists them all, so a key holding every available permission counts too.
 */
export function hasFullAccess(
  permissions: string[],
  available: ApiKeyPermission[] | undefined,
): boolean {
  if (!permissions.length) return true;
  return Boolean(
    available?.length &&
    available.every((entry) => permissions.includes(entry.value)),
  );
}

/** "Full access" or "3 permissions". */
export function accessSummary(
  permissions: string[],
  available: ApiKeyPermission[] | undefined,
): string {
  return hasFullAccess(permissions, available)
    ? "Full access"
    : count(permissions.length, "permission");
}

/** Expired keys are rejected even while they are still active. */
export function isExpired(key: ApiKey, now = Date.now()): boolean {
  return Boolean(key.expires_at && new Date(key.expires_at).getTime() <= now);
}

/** "Active", "Expired" or "Revoked". */
export function keyStatus(key: ApiKey, now = Date.now()): string {
  if (!key.is_active) return "Revoked";
  return isExpired(key, now) ? "Expired" : "Active";
}

/** "Expires Nov 4, 2026" or "Never expires". */
export function expiryLabel(key: ApiKey): string {
  return key.expires_at
    ? `${isExpired(key) ? "Expired" : "Expires"} ${formatDate(key.expires_at)}`
    : "Never expires";
}

/** "kai_ab12… · Full access · Never expires" for the keys list. */
export function keySubtitle(
  key: ApiKey,
  available: ApiKeyPermission[] | undefined,
): string {
  return [
    `${key.key_prefix}…`,
    accessSummary(key.permissions, available),
    expiryLabel(key),
  ].join(" · ");
}

export const EXPIRY_CHOICES = [
  { value: "never", label: "Never" },
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "custom", label: "Custom date" },
] as const;

export type ExpiryChoice = (typeof EXPIRY_CHOICES)[number]["value"];

/** Today as "YYYY-MM-DD" in the device's time zone, optionally shifted by days. */
export function localDay(offsetDays = 0, now = Date.now()): string {
  return wallClockValue("date", new Date(now + offsetDays * DAY_MS));
}

/**
 * `expires_at` for the create payload, ISO 8601 in UTC: a preset counts
 * days from now, a custom date ends with that local day; `null` never
 * expires.
 */
export function expiresAt(
  choice: ExpiryChoice,
  customDate: string,
  now = Date.now(),
): string | null {
  if (choice === "never") return null;
  if (choice === "custom") {
    const day = wallClockDate("date", customDate);
    day.setHours(23, 59, 59, 0);
    return day.toISOString();
  }
  return new Date(now + Number(choice) * DAY_MS).toISOString();
}

/** A custom expiry must be a real date from today on. */
export function isValidExpiryDate(value: string, now = Date.now()): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && value >= localDay(0, now);
}

/** A key's restriction state while editing: everything, or exactly `selected`. */
export interface PermissionForm {
  full: boolean;
  selected: string[];
}

/** The editable state of a stored key's permissions. */
export function permissionForm(
  permissions: string[],
  available: ApiKeyPermission[],
): PermissionForm {
  return hasFullAccess(permissions, available)
    ? { full: true, selected: [] }
    : { full: false, selected: [...permissions].sort() };
}

/**
 * Turns full access on (nothing selected) or off; restricting starts with
 * every permission selected, so that nothing is lost by accident.
 */
export function setFullAccess(
  form: PermissionForm,
  full: boolean,
  available: ApiKeyPermission[],
): PermissionForm {
  return full
    ? { full: true, selected: [] }
    : {
        full: false,
        selected: form.selected.length
          ? form.selected
          : available.map((entry) => entry.value).sort(),
      };
}

/**
 * Selects or clears one permission. The last one cannot be cleared: an
 * empty list would grant every permission.
 */
export function togglePermission(
  form: PermissionForm,
  value: string,
  on: boolean,
): PermissionForm {
  if (on)
    return form.selected.includes(value)
      ? form
      : { ...form, selected: [...form.selected, value].sort() };
  if (form.selected.length === 1 && form.selected[0] === value) return form;
  return {
    ...form,
    selected: form.selected.filter((entry) => entry !== value),
  };
}

/**
 * The `permissions` to send: `[]` for full access, otherwise the selection;
 * `null` when a restricted key has nothing selected, since `[]` would grant
 * everything.
 */
export function permissionsPayload(form: PermissionForm): string[] | null {
  if (form.full) return [];
  return form.selected.length ? form.selected : null;
}
