import type {
  Plugin,
  PluginCredentialRequirement,
  PluginMcpServer,
} from "@opencuria/client";
import { pluginAvailability } from "@opencuria/chat-core/plugins";
import { credentialTypeLabel } from "./credentials";
import { count } from "../../format";

/** "STDIO", "Streamable HTTP", "SSE" or "Built-in". */
export function transportLabel(transport: string): string {
  switch (transport) {
    case "builtin":
      return "Built-in";
    case "streamable_http":
      return "Streamable HTTP";
    case "stdio":
      return "STDIO";
    default:
      return transport.toUpperCase();
  }
}

/** The command line of a stdio server, otherwise its URL. */
export function mcpEndpoint(
  server: Pick<PluginMcpServer, "transport" | "command" | "args" | "url">,
): string {
  return server.transport === "stdio"
    ? [server.command, ...(server.args ?? [])].filter(Boolean).join(" ")
    : server.url;
}

/** "STDIO · npx -y @playwright/mcp" or "Streamable HTTP · OAuth · https://…". */
export function mcpSubtitle(server: PluginMcpServer): string {
  return [
    transportLabel(server.transport),
    server.auth_type === "oauth" ? "OAuth" : "",
    mcpEndpoint(server),
  ]
    .filter(Boolean)
    .join(" · ");
}

/** "Global" or "Organization". */
export function pluginOwner(plugin: Plugin): string {
  return plugin.is_global ? "Global" : "Organization";
}

/** "Global · 2 skills · 1 MCP server · 1 service"; empty parts are left out. */
export function pluginSubtitle(plugin: Plugin): string {
  const skills = plugin.skills?.length ?? 0;
  const servers = plugin.mcp_servers?.length ?? 0;
  const services = plugin.credential_requirements.length;
  return [
    pluginOwner(plugin),
    skills ? count(skills, "skill") : "",
    servers ? count(servers, "MCP server") : "",
    services ? count(services, "service") : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** "Active", "Inactive" or "Unpublished" for the organization. */
export function pluginStatus(plugin: Plugin): string {
  return pluginAvailability(plugin);
}

/** "Required · ENV" plus the requirement's description. */
export function requirementSubtitle(
  requirement: PluginCredentialRequirement,
): string {
  const summary = `${requirement.required ? "Required" : "Optional"} · ${credentialTypeLabel(requirement.credential_type)}`;
  return requirement.description
    ? `${summary} · ${requirement.description}`
    : summary;
}
