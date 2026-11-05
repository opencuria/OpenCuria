/**
 * Plugin catalog types shared by the web and native clients. A plugin
 * bundles prompt skills, MCP servers and the credential services they need.
 */

export type DesktopActivation = "server_start" | "first_tool";
export interface PluginResources {
  desktop?: { activation?: DesktopActivation };
}
export type PluginMcpTransport = "stdio" | "streamable_http" | "sse";
/** `builtin` only exists on the seeded OpenCuria plugin. */
export type PluginMcpServerTransport = PluginMcpTransport | "builtin";
export type PluginCredentialServiceType =
  | "env"
  | "file"
  | "ssh_key"
  | "mcp_oauth";
export type PluginMcpAuthType = "none" | "oauth";

export interface PluginSkill {
  id: string;
  name: string;
  slug: string;
  body: string;
  position: number;
}

export interface PluginSkillIn {
  name: string;
  slug?: string;
  body: string;
  position?: number;
}

export interface PluginMcpToolPolicy {
  exposed_tools?: string[] | null;
  internal_tools?: string[];
  desktop_tools?: string[] | null;
  execution_tools?: string[];
  on_run_end?: string;
  desktop_kind?: "mcp" | "computeruse";
  exclusive_input?: boolean;
}

export interface PluginMcpServer {
  id: string;
  name: string;
  slug: string;
  transport: PluginMcpServerTransport;
  command: string;
  args: string[];
  cwd: string;
  resources?: PluginResources;
  tool_policy?: PluginMcpToolPolicy;
  runtime_artifact?: { id: string; version: string } | null;
  env: Record<string, string>;
  url: string;
  headers: Record<string, string>;
  startup_timeout_seconds: number;
  request_timeout_seconds: number;
  auth_type: PluginMcpAuthType;
  oauth_requirement_key: string;
}

export interface PluginMcpServerIn {
  name: string;
  slug?: string;
  transport?: PluginMcpTransport;
  command?: string;
  args?: string[];
  cwd?: string;
  resources?: PluginResources;
  tool_policy?: PluginMcpToolPolicy;
  runtime_artifact?: { id: string; version: string } | null;
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  startup_timeout_seconds?: number;
  request_timeout_seconds?: number;
  auth_type?: PluginMcpAuthType;
  oauth_requirement_key?: string;
}

export interface PluginCredentialRequirementIn {
  key: string;
  description?: string;
  required?: boolean;
  service_id: string;
}

export interface PluginCredentialRequirement {
  id: string;
  key: string;
  description: string;
  required: boolean;
  service_id: string;
  service_name: string;
  service_slug: string;
  credential_type: PluginCredentialServiceType | string;
}

export interface Plugin {
  id: string;
  name: string;
  slug: string;
  description: string;
  enabled: boolean;
  published: boolean;
  /** `null` for global plugins, which only staff can edit. */
  organization_id: string | null;
  is_global: boolean;
  /** Activated for the organization, so workspaces can enable it. */
  org_enabled: boolean;
  skills?: PluginSkill[];
  mcp_servers?: PluginMcpServer[];
  credential_requirements: PluginCredentialRequirement[];
  created_at?: string;
  updated_at?: string;
}

export interface PluginCreateIn {
  name: string;
  slug?: string;
  description?: string;
  enabled?: boolean;
  published?: boolean;
  skills?: PluginSkillIn[];
  mcp_servers?: PluginMcpServerIn[];
  credential_requirements?: PluginCredentialRequirementIn[];
}

/** Lists that are sent replace the stored lists; omitted lists are kept. */
export interface PluginUpdateIn {
  name?: string;
  slug?: string;
  description?: string;
  enabled?: boolean;
  published?: boolean;
  skills?: PluginSkillIn[] | null;
  mcp_servers?: PluginMcpServerIn[] | null;
  credential_requirements?: PluginCredentialRequirementIn[] | null;
}

/** A service a credential belongs to (env var, file, SSH key or MCP OAuth). */
export interface CredentialService {
  id: string;
  name: string;
  slug: string;
  description: string;
  credential_type: PluginCredentialServiceType | string;
  /** `null` for global catalog services. */
  organization_id?: string | null;
  env_var_name: string;
  target_path: string;
  label: string;
  oauth_server_url: string;
  is_active: boolean;
}

/** "Unpublished", "Active" or "Inactive" for the organization. */
export function pluginAvailability(
  plugin: Pick<Plugin, "enabled" | "published" | "org_enabled">,
): "Unpublished" | "Active" | "Inactive" {
  if (!plugin.enabled || !plugin.published) return "Unpublished";
  return plugin.org_enabled ? "Active" : "Inactive";
}

/** Activation requires a published, enabled plugin; turning off always works. */
export function canTogglePlugin(
  plugin: Pick<Plugin, "enabled" | "published" | "org_enabled">,
): boolean {
  return plugin.org_enabled || (plugin.enabled && plugin.published);
}
