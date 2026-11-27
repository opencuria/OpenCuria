/**
 * Plugin catalog types shared by the web and native clients. A plugin
 * bundles skills (Markdown instructions agents load on demand) and MCP
 * servers (tools), plus the credentials they need. Plugins are personal,
 * shared with the organization, or built into OpenCuria.
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
/** Who owns a plugin: the user, the organization or OpenCuria. */
export type PluginScope = "personal" | "organization" | "builtin";

export interface PluginSkill {
  id: string;
  name: string;
  slug: string;
  /** What the skill does and when to use it; agents see only this up front. */
  description: string;
  body: string;
  position: number;
}

export interface PluginSkillIn {
  /** Keeps an existing skill (and chat selections of it) across renames. */
  id?: string;
  name: string;
  slug?: string;
  description?: string;
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
  auth_type: PluginMcpAuthType;
  oauth_requirement_key: string;
}

export interface PluginMcpServerIn {
  /** Keeps an existing server and its saved secrets across renames. */
  id?: string;
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
  auth_type?: PluginMcpAuthType;
  oauth_requirement_key?: string;
}

/**
 * A credential service the plugin needs (by `service_id`, or a
 * `new_service` created with it), optionally with the credential it always
 * uses (`credential_id`) or a `value` stored as that credential. Without
 * one, each workspace attaches its own credential of the service.
 */
export interface PluginCredentialRequirementIn {
  key: string;
  description?: string;
  required?: boolean;
  service_id?: string;
  new_service?: { name: string; env_var_name?: string };
  credential_id?: string | null;
  value?: string;
}

export interface PluginCredentialRequirement {
  id: string;
  key: string;
  description: string;
  required: boolean;
  service_id: string;
  service_name: string;
  service_slug: string;
  credential_type: PluginCredentialServiceType;
  /** The credential the plugin uses; `null`: each workspace attaches one. */
  credential_id: string | null;
  credential_name: string;
  credential_scope: "personal" | "organization" | "";
}

export interface Plugin {
  id: string;
  name: string;
  slug: string;
  description: string;
  scope: PluginScope;
  enabled: boolean;
  published: boolean;
  /** `null` for built-in and personal plugins. */
  organization_id: string | null;
  is_global: boolean;
  /** Released for the organization (built-in and organization plugins). */
  org_enabled: boolean;
  /** Can be enabled in the user's workspaces. */
  available: boolean;
  /** The user may change or delete it. */
  editable: boolean;
  skills: PluginSkill[];
  mcp_servers: PluginMcpServer[];
  credential_requirements: PluginCredentialRequirement[];
  created_at: string;
  updated_at: string;
}

export interface PluginCreateIn {
  name: string;
  slug?: string;
  /** `organization` needs an organization admin. */
  scope?: "personal" | "organization";
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

/** A skill the user can select for a chat or scheduled task. */
export interface SelectableSkill {
  id: string;
  name: string;
  description: string;
  plugin_id: string;
  plugin_name: string;
  scope: PluginScope;
}

const BOUND_PLACEHOLDER = /^\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}$/;

/** Requirement key of a value that is exactly `{{credential.KEY}}`, else `null`. */
export function boundCredentialKey(value: string): string | null {
  return BOUND_PLACEHOLDER.exec(value)?.[1] ?? null;
}

/** Variables (stdio) or headers (URL) of a server with what they hold. */
export function serverVariableList(
  server: Pick<PluginMcpServer, "transport" | "env" | "headers">,
): Array<{ name: string; secret: boolean }> {
  const mapping = server.transport === "stdio" ? server.env : server.headers;
  return Object.entries(mapping ?? {}).map(([name, value]) => ({
    name,
    secret: boundCredentialKey(value) !== null,
  }));
}

/** "Uses “Work token”" or "Each workspace adds its own". */
export function credentialSourceLabel(
  requirement: Pick<
    PluginCredentialRequirement,
    "credential_id" | "credential_name"
  >,
): string {
  return requirement.credential_id
    ? `Uses “${requirement.credential_name}”`
    : "Each workspace adds its own";
}

/** "Personal", "Organization" or "Built-in". */
export function pluginScopeLabel(scope: PluginScope): string {
  if (scope === "personal") return "Personal";
  return scope === "organization" ? "Organization" : "Built-in";
}

/** Section order of the plugin catalog: own plugins first. */
export const PLUGIN_SCOPES: PluginScope[] = [
  "personal",
  "organization",
  "builtin",
];

/** Plugins of one scope, by name. */
export function pluginsOfScope<P extends Pick<Plugin, "scope" | "name">>(
  plugins: P[],
  scope: PluginScope,
): P[] {
  return plugins
    .filter((plugin) => plugin.scope === scope)
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** "1 skill · 2 MCP servers"; empty parts are left out. */
export function pluginContents(
  plugin: Pick<Plugin, "skills" | "mcp_servers">,
): string {
  const skills = plugin.skills?.length ?? 0;
  const servers = plugin.mcp_servers?.length ?? 0;
  return [
    skills ? `${skills} ${skills === 1 ? "skill" : "skills"}` : "",
    servers ? `${servers} ${servers === 1 ? "MCP server" : "MCP servers"}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * Availability for admins: "Unpublished", "Available" (released for the
 * organization) or "Not released". Personal plugins are always "Available".
 */
export function pluginAvailability(
  plugin: Pick<Plugin, "enabled" | "published" | "org_enabled"> & {
    scope?: PluginScope;
  },
): "Unpublished" | "Available" | "Not released" {
  if (plugin.scope === "personal") return "Available";
  if (!plugin.enabled || !plugin.published) return "Unpublished";
  return plugin.org_enabled ? "Available" : "Not released";
}

/** Releasing requires a published, enabled plugin; withdrawing always works. */
export function canTogglePlugin(
  plugin: Pick<Plugin, "enabled" | "published" | "org_enabled"> & {
    scope?: PluginScope;
  },
): boolean {
  if (plugin.scope === "personal") return false;
  return plugin.org_enabled || (plugin.enabled && plugin.published);
}

const SAFE_ARG = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** Shell-quote one argument for display (`'a b'`, `''`). */
export function quoteArgument(arg: string): string {
  if (arg && SAFE_ARG.test(arg)) return arg;
  return `'${arg.replace(/'/g, `'"'"'`)}'`;
}

/** The command line of a stdio server, otherwise its URL. */
export function mcpEndpoint(
  server: Pick<PluginMcpServer, "transport" | "command" | "args" | "url">,
): string {
  return server.transport === "stdio"
    ? [server.command, ...(server.args ?? []).map(quoteArgument)]
        .filter(Boolean)
        .join(" ")
    : server.url;
}

/** "Command", "Streamable HTTP", "SSE" or "Built-in". */
export function mcpTransportLabel(transport: string): string {
  switch (transport) {
    case "builtin":
      return "Built-in";
    case "streamable_http":
      return "Streamable HTTP";
    case "stdio":
      return "Command";
    default:
      return transport.toUpperCase();
  }
}
