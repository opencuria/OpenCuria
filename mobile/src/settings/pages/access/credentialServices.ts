import { slugify } from "@opencuria/chat-core/pluginForms";
import type {
  CredentialService,
  CredentialServiceCreateInput,
} from "@opencuria/client";

export type CredentialServiceType =
  CredentialServiceCreateInput["credential_type"];

export const SERVICE_TYPES: Array<{
  value: CredentialServiceType;
  label: string;
}> = [
  { value: "env", label: "Environment variable" },
  { value: "file", label: "Credential file" },
  { value: "ssh_key", label: "SSH key pair" },
  { value: "mcp_oauth", label: "MCP OAuth" },
];

/** "Environment variable", "Credential file", "SSH key pair" or "MCP OAuth". */
export function serviceTypeLabel(type: string): string {
  return SERVICE_TYPES.find((entry) => entry.value === type)?.label ?? type;
}

/** "Organization-owned" or "Global". */
export function serviceOwnership(service: CredentialService): string {
  return service.organization_id ? "Organization-owned" : "Global";
}

/** Type and ownership, then the description on its own line. */
export function serviceSubtitle(service: CredentialService): string {
  const summary = `${serviceTypeLabel(service.credential_type)} · ${serviceOwnership(service)}`;
  return service.description ? `${summary}\n${service.description}` : summary;
}

export interface CredentialServiceForm {
  name: string;
  type: CredentialServiceType;
  envVarName: string;
  targetPath: string;
  oauthServerUrl: string;
  label: string;
  description: string;
}

export function emptyServiceForm(): CredentialServiceForm {
  return {
    name: "",
    type: "env",
    envVarName: "",
    targetPath: "",
    oauthServerUrl: "",
    label: "",
    description: "",
  };
}

const ENV_VAR = /^[A-Z_][A-Z0-9_]*$/;

/** Problem with a file target path (mirrors the backend), or `null`. */
export function targetPathError(value: string): string | null {
  const path = value.trim();
  if (!path) return "Enter the file path in the workspace.";
  if (path.includes("\0")) return "The path must not contain null bytes.";
  if (path === "~" || path === "${HOME}" || path === "/" || path.endsWith("/"))
    return "The path must point to a file, not a folder.";
  const relative = path.startsWith("~/")
    ? path.slice(2)
    : path.startsWith("${HOME}/")
      ? path.slice("${HOME}/".length)
      : path.startsWith("/")
        ? path.slice(1)
        : path;
  if (
    relative
      .split("/")
      .some((part) => part === "" || part === "." || part === "..")
  )
    return "The path must not contain empty, “.” or “..” segments.";
  return null;
}

/**
 * Problem with an MCP OAuth endpoint, or `null`: a fixed HTTPS URL with a
 * host and no credentials, query or fragment. Parsed by hand because React
 * Native's URL does not validate.
 */
export function oauthServerUrlError(value: string): string | null {
  const url = value.trim();
  const match = /^https:\/\/([^/?#]*)([^?#]*)$/i.exec(url);
  const authority = match?.[1] ?? "";
  const host = /^(\[[0-9a-f:.]+\]|[^:[\]]+)(?::(\d{1,5}))?$/i.exec(authority);
  const port = host?.[2] === undefined ? null : Number(host[2]);
  if (
    !match ||
    !host ||
    authority.includes("@") ||
    // Control characters and spaces (the backend rejects everything below "!").
    // eslint-disable-next-line no-control-regex
    /[\u0000- \u007f]/.test(url) ||
    (port !== null && (port < 1 || port > 65535))
  )
    return "Use an HTTPS URL without credentials, query or fragment.";
  return null;
}

/** Validation problems keyed by field; empty required fields only block saving. */
export function validateServiceForm(
  form: CredentialServiceForm,
): Partial<
  Record<"name" | "envVarName" | "targetPath" | "oauthServerUrl", string>
> {
  const errors: Partial<
    Record<"name" | "envVarName" | "targetPath" | "oauthServerUrl", string>
  > = {};
  if (form.name.trim() && !slugify(form.name))
    errors.name = "Use letters or digits in the name.";
  if (form.type === "env" && form.envVarName.trim()) {
    if (!ENV_VAR.test(form.envVarName.trim().toUpperCase()))
      errors.envVarName =
        "Use letters, digits and underscores, starting with a letter, e.g. GITHUB_TOKEN.";
  }
  if (form.type === "file" && form.targetPath.trim()) {
    const error = targetPathError(form.targetPath);
    if (error) errors.targetPath = error;
  }
  if (form.type === "mcp_oauth" && form.oauthServerUrl.trim()) {
    const error = oauthServerUrlError(form.oauthServerUrl);
    if (error) errors.oauthServerUrl = error;
  }
  return errors;
}

/** Whether the form can be created: required fields filled and nothing invalid. */
export function isServiceFormValid(form: CredentialServiceForm): boolean {
  if (!form.name.trim() || Object.keys(validateServiceForm(form)).length)
    return false;
  switch (form.type) {
    case "env":
      return Boolean(form.envVarName.trim());
    case "file":
      return Boolean(form.targetPath.trim());
    case "mcp_oauth":
      return Boolean(form.oauthServerUrl.trim());
    default:
      return true;
  }
}

/** The create payload; the backend derives the slug from the name. */
export function serviceCreateInput(
  form: CredentialServiceForm,
): CredentialServiceCreateInput {
  return {
    name: form.name.trim(),
    slug: "",
    description: form.description.trim(),
    credential_type: form.type,
    label: form.label.trim(),
    ...(form.type === "env"
      ? { env_var_name: form.envVarName.trim().toUpperCase() }
      : {}),
    ...(form.type === "file" ? { target_path: form.targetPath.trim() } : {}),
    ...(form.type === "mcp_oauth"
      ? { oauth_server_url: form.oauthServerUrl.trim() }
      : {}),
  };
}
