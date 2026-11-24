import type { PluginMcpForm } from "@opencuria/chat-core/pluginForms";
import {
  credentialSourceLabel,
  mcpEndpoint,
  mcpTransportLabel,
  pluginAvailability,
  pluginContents,
  serverVariableList,
} from "@opencuria/chat-core/plugins";
import type {
  Plugin,
  PluginCredentialRequirement,
  PluginMcpServer,
} from "@opencuria/client";
import type { NativeFormTone } from "../../../ui/NativeForm.types";
import { count } from "../../format";
import { credentialTypeLabel } from "./credentials";

/**
 * "1 skill · 2 MCP servers"; admins also see whether a shared plugin is
 * available in the organization (release is an admin setting).
 */
export function pluginSummary(
  plugin: Plugin,
  isAdmin: boolean,
): { text: string; tone: NativeFormTone } {
  const availability =
    isAdmin && plugin.scope !== "personal" ? pluginAvailability(plugin) : "";
  return {
    text: [pluginContents(plugin), availability].filter(Boolean).join(" · "),
    tone: availability === "Available" ? "success" : "muted",
  };
}

/** "Command · npx -y pkg · 1 secret" or "Streamable HTTP · https://…". */
export function mcpServerSubtitle(server: PluginMcpServer): string {
  const secrets = serverVariableList(server).filter(
    (variable) => variable.secret,
  ).length;
  return [
    mcpTransportLabel(server.transport),
    mcpEndpoint(server),
    secrets ? count(secrets, "secret") : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The same summary for a server being edited. */
export function mcpFormSubtitle(mcp: PluginMcpForm): string {
  return mcp.kind === "command"
    ? ["Command", mcp.commandLine.trim()].filter(Boolean).join(" · ")
    : [mcpTransportLabel(mcp.transport), mcp.url.trim()]
        .filter(Boolean)
        .join(" · ");
}

/** "Uses “Work token”" or "Required · Each workspace adds its own", plus the description. */
export function requirementSubtitle(
  requirement: PluginCredentialRequirement,
): string {
  const summary = requirement.credential_id
    ? credentialSourceLabel(requirement)
    : `${requirement.required ? "Required" : "Optional"} · ${credentialTypeLabel(requirement.credential_type)} · ${credentialSourceLabel(requirement)}`;
  return requirement.description
    ? `${summary} · ${requirement.description}`
    : summary;
}
