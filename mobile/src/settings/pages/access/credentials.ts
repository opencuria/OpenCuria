import type {
  Credential,
  CredentialService,
  CredentialUpdateInput,
} from "@opencuria/client";
import type { NativeFormTone } from "../../../ui/NativeForm.types";

/** Short credential type label: "ENV", "File", "SSH key" or "OAuth". */
export function credentialTypeLabel(type: string): string {
  switch (type) {
    case "mcp_oauth":
      return "OAuth";
    case "ssh_key":
      return "SSH key";
    case "file":
      return "File";
    default:
      return "ENV";
  }
}

/** "Organization" or "Personal". */
export function credentialScopeLabel(credential: Credential): string {
  return credential.scope === "organization" ? "Organization" : "Personal";
}

/** The connection state of an OAuth credential and its color. */
export function oauthStatus(credential: Credential): {
  label: string;
  tone: NativeFormTone;
} {
  if (credential.oauth_connected)
    return { label: "Connected", tone: "success" };
  if (credential.oauth_reconnect_required)
    return { label: "Reconnect required", tone: "warning" };
  return { label: "Not connected", tone: "warning" };
}

/** "GitHub · Personal · ENV" for the credentials list. */
export function credentialSubtitle(credential: Credential): string {
  return [
    credential.service_name,
    credentialScopeLabel(credential),
    credentialTypeLabel(credential.credential_type),
  ].join(" · ");
}

/** Owners manage personal credentials; admins manage organization credentials. */
export function canManageCredential(
  credential: Credential,
  userId: number,
  isAdmin: boolean,
): boolean {
  return credential.scope === "personal"
    ? credential.created_by_id === userId
    : isAdmin;
}

/** OAuth credentials can be reconnected when disconnected or expired. */
export function canReconnect(credential: Credential): boolean {
  return (
    credential.credential_type === "mcp_oauth" &&
    (credential.oauth_reconnect_required || !credential.oauth_connected)
  );
}

/** OAuth credentials with a (possibly expired) grant can be disconnected. */
export function canDisconnect(credential: Credential): boolean {
  return (
    credential.credential_type === "mcp_oauth" &&
    Boolean(credential.oauth_connected || credential.oauth_reconnect_required)
  );
}

/** Only environment and file credentials take a new secret value. */
export function acceptsValue(type: string): boolean {
  return type === "env" || type === "file";
}

/**
 * The fields of an edit that changed: a trimmed new name and a non-empty
 * value; `null` when nothing would change or the name is empty.
 */
export function credentialUpdate(
  credential: Credential,
  name: string,
  value: string,
): CredentialUpdateInput | null {
  const trimmed = name.trim();
  if (!trimmed) return null;
  const input: CredentialUpdateInput = {};
  if (trimmed !== credential.name) input.name = trimmed;
  if (value && acceptsValue(credential.credential_type)) input.value = value;
  return Object.keys(input).length ? input : null;
}

/**
 * The webapp page that finishes an OAuth connection: the backend binds the
 * flow to a browser session, so the app hands it over to the browser.
 */
export function oauthHandoffUrl(
  serverUrl: string,
  target: { serviceId: string } | { credentialId: string },
): string {
  const base = serverUrl.replace(/\/+$/, "");
  const query =
    "serviceId" in target
      ? `add_credential=${encodeURIComponent(target.serviceId)}`
      : `reconnect_credential=${encodeURIComponent(target.credentialId)}`;
  return `${base}/?settings=credentials&${query}`;
}

/** Why a preselected service cannot take new credentials, or `null`. */
export function serviceUnavailableReason(
  service: CredentialService | undefined,
): string | null {
  if (!service)
    return "That credential service is unavailable in this organization. Choose an active service or ask an admin.";
  if (!service.is_active)
    return `${service.name} is inactive for this organization. Ask an admin to activate it before creating credentials.`;
  return null;
}
