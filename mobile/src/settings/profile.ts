import type { AuthUser, Organization } from "@opencuria/client";

/** The name shown for the signed-in user: full name, else the email address. */
export function profileName(
  user: Pick<AuthUser, "first_name" | "last_name" | "email">,
): string {
  const name = [user.first_name, user.last_name]
    .map((part) => part?.trim())
    .filter(Boolean)
    .join(" ");
  return name || user.email || "Account";
}

/** Up to two initials, e.g. "AL" for Ada Lovelace or "A" for ada@example.com. */
export function profileInitials(
  user: Pick<AuthUser, "first_name" | "last_name" | "email">,
): string {
  const first = user.first_name?.trim();
  const last = user.last_name?.trim();
  if (first || last)
    return `${first?.[0] ?? ""}${last?.[0] ?? ""}`.toUpperCase();
  const local = (user.email ?? "").split("@")[0] ?? "";
  const parts = local.split(/[._-]+/).filter(Boolean);
  const letters =
    parts.length > 1 ? `${parts[0]![0]}${parts[1]![0]}` : (local[0] ?? "?");
  return letters.toUpperCase();
}

/** Org admins manage shared settings, runners and credential services. */
export function isOrganizationAdmin(
  organizations: Organization[],
  organizationId: string | null | undefined,
): boolean {
  return (
    organizations.find((organization) => organization.id === organizationId)
      ?.role === "admin"
  );
}

/** Avatar colors: mid-tone system hues that keep white initials readable. */
const AVATAR_COLORS = [
  "#E5679E",
  "#8E6CEF",
  "#3E8EF7",
  "#2FA88F",
  "#E8833A",
  "#C1564F",
];

/** A stable avatar color per account, like ChatGPT's profile circle. */
export function avatarColor(user: Pick<AuthUser, "email">): string {
  let hash = 0;
  for (const character of user.email ?? "")
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length]!;
}
