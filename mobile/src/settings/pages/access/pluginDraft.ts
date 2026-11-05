import {
  slugify,
  type PluginFormModel,
  type PluginMcpForm,
  type PluginMcpTransportOption,
  type PluginRequirementForm,
  type PluginServiceTypeOption,
  type PluginSkillForm,
} from "@opencuria/chat-core/pluginForms";
import type { CredentialService } from "@opencuria/client";

/**
 * A plugin being created (`pluginId` null) or edited. The editor and its
 * skill, MCP server and requirement pages edit one shared draft; every
 * change returns a new draft so native snapshots stay immutable.
 */
export interface PluginDraft {
  pluginId: string | null;
  form: PluginFormModel;
}

type Entries = {
  skills: PluginSkillForm;
  mcps: PluginMcpForm;
  requirements: PluginRequirementForm;
};

/** Replaces one skill, MCP server or requirement, found by its uid. */
export function updateEntry<K extends keyof Entries>(
  form: PluginFormModel,
  list: K,
  uid: string,
  update: (entry: Entries[K]) => Entries[K],
): PluginFormModel {
  const entries = form[list] as Entries[K][];
  return {
    ...form,
    [list]: entries.map((entry) => (entry.uid === uid ? update(entry) : entry)),
  };
}

/** Removes one skill, MCP server or requirement. */
export function removeEntry(
  form: PluginFormModel,
  list: keyof Entries,
  uid: string,
): PluginFormModel {
  const entries = form[list] as Array<{ uid: string }>;
  return { ...form, [list]: entries.filter((entry) => entry.uid !== uid) };
}

/**
 * Switches an MCP server's transport and clears what the new transport
 * forbids, so no stale value hides in the payload: stdio has no URL,
 * headers or OAuth; HTTP and SSE have no command, arguments, environment
 * or managed desktop.
 */
export function setMcpTransport(
  form: PluginFormModel,
  uid: string,
  transport: PluginMcpTransportOption,
): PluginFormModel {
  return updateEntry(form, "mcps", uid, (mcp) => {
    if (mcp.transport === transport) return mcp;
    return transport === "stdio"
      ? {
          ...mcp,
          transport,
          url: "",
          headers: [],
          authType: "none",
          oauthRequirementKey: "",
        }
      : {
          ...mcp,
          transport,
          desktop: "none",
          command: "",
          argsText: "",
          env: [],
        };
  });
}

/** Turns MCP OAuth on or off; off forgets the requirement. */
export function setMcpAuthentication(
  form: PluginFormModel,
  uid: string,
  authType: "none" | "oauth",
): PluginFormModel {
  return updateEntry(form, "mcps", uid, (mcp) => ({
    ...mcp,
    authType,
    oauthRequirementKey: authType === "none" ? "" : mcp.oauthRequirementKey,
  }));
}

/**
 * Links an OAuth MCP server to a requirement; the server then uses the
 * endpoint of the requirement's OAuth service, which must match exactly.
 */
export function setMcpOAuthRequirement(
  form: PluginFormModel,
  uid: string,
  requirementKey: string,
  services: CredentialService[],
): PluginFormModel {
  const requirement = form.requirements.find(
    (entry) => entry.reqKey === requirementKey,
  );
  const service = services.find((entry) => entry.id === requirement?.serviceId);
  return updateEntry(form, "mcps", uid, (mcp) => ({
    ...mcp,
    oauthRequirementKey: requirementKey,
    url:
      service?.credential_type === "mcp_oauth"
        ? service.oauth_server_url
        : mcp.url,
  }));
}

function serviceType(value: string): PluginServiceTypeOption {
  return value === "file" || value === "ssh_key" || value === "mcp_oauth"
    ? value
    : "env";
}

/** `api_key` from "API Key": the default requirement key for a service. */
export function requirementKeyFor(serviceName: string): string {
  return slugify(serviceName).replace(/-/g, "_");
}

/**
 * Points a requirement at a credential service: takes over its type, fills
 * an empty key from the service name and, for MCP OAuth, links the OAuth
 * server (the only one, if none is linked yet) to the service's endpoint.
 */
export function setRequirementService(
  form: PluginFormModel,
  uid: string,
  service: CredentialService,
): PluginFormModel {
  const current = form.requirements.find((entry) => entry.uid === uid);
  if (!current) return form;
  const reqKey = current.reqKey.trim()
    ? current.reqKey
    : requirementKeyFor(service.name);
  const next = updateEntry(form, "requirements", uid, (entry) => ({
    ...entry,
    serviceId: service.id,
    credentialType: serviceType(service.credential_type),
    reqKey,
  }));
  if (service.credential_type !== "mcp_oauth") return next;
  let linked = next.mcps.filter((mcp) => mcp.oauthRequirementKey === reqKey);
  const oauthRequirements = next.requirements.filter(
    (entry) => entry.credentialType === "mcp_oauth",
  );
  const oauthServers = next.mcps.filter((mcp) => mcp.authType === "oauth");
  if (
    !linked.length &&
    oauthRequirements.length === 1 &&
    oauthServers.length === 1
  )
    linked = oauthServers;
  const uids = new Set(linked.map((mcp) => mcp.uid));
  return {
    ...next,
    mcps: next.mcps.map((mcp) =>
      uids.has(mcp.uid)
        ? {
            ...mcp,
            oauthRequirementKey: reqKey,
            url: service.oauth_server_url,
          }
        : mcp,
    ),
  };
}

/** How `validatePluginForm` names a skill, e.g. "Skill #2". */
export function skillLabel(index: number): string {
  return `Skill #${index + 1}`;
}

/** How `validatePluginForm` names an MCP server. */
export function mcpLabel(mcp: PluginMcpForm, index: number): string {
  const name = mcp.name.trim();
  return name ? `MCP server "${name}"` : `MCP server #${index + 1}`;
}

/** How `validatePluginForm` names a requirement. */
export function requirementLabel(
  requirement: PluginRequirementForm,
  index: number,
): string {
  const key = requirement.reqKey.trim();
  return key ? `Requirement "${key}"` : `Requirement #${index + 1}`;
}

/** The validation errors of one entry, without its label prefix. */
export function entryErrors(errors: string[], label: string): string[] {
  const prefix = `${label}: `;
  return errors
    .filter((error) => error.startsWith(prefix))
    .map((error) => {
      const text = error.slice(prefix.length);
      return text.charAt(0).toUpperCase() + text.slice(1);
    });
}

let drafts = 0;

/** A route-safe key for a new draft. */
export function newDraftKey(): string {
  drafts += 1;
  return `${Date.now().toString(36)}${drafts}`;
}
