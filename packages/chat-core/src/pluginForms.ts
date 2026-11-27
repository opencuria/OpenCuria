/**
 * Form model + helpers for the plugin editors of the web and native clients.
 *
 * Keeps the editors thin: conversion between `Plugin` API objects and
 * editable rows, edits that keep dependent fields consistent, import of
 * pasted MCP configurations (the `mcpServers` JSON of Claude, Cursor and
 * VS Code, a command line or a URL) and SKILL.md files, payload
 * normalization and client-side validation. Edits never mutate their
 * input; they return the next form.
 *
 * Slugs are never user-editable: payloads send empty slugs and the
 * backend derives them from the names. Timeouts are not configurable.
 */

import type {
  CredentialService,
  Plugin,
  PluginCreateIn,
  PluginCredentialRequirement,
  PluginCredentialRequirementIn,
  PluginCredentialServiceType,
  PluginMcpAuthType,
  PluginMcpServerIn,
  PluginMcpToolPolicy,
  PluginSkillIn,
  PluginUpdateIn,
} from "./plugins";
import { boundCredentialKey, quoteArgument } from "./plugins";

/** Exact placeholder syntax for per-user credentials in values. */
export const PLACEHOLDER_EXAMPLE = "{{credential.KEY}}";

/** How an MCP server is reached: a command in the workspace or a URL. */
export type McpKind = "command" | "url";
export type McpUrlTransport = "streamable_http" | "sse";
export type PluginServiceTypeOption = PluginCredentialServiceType;

/**
 * Where a secret variable gets its value: a value stored as a credential
 * the plugin uses, an existing credential, or each workspace's own.
 */
export type SecretSource = "value" | "credential" | "per_user";

/**
 * One environment variable (command) or header (URL). A secret variable is
 * a credential requirement of the plugin: its value lives in a credential,
 * never in the plugin.
 */
export interface VariableRow {
  uid: string;
  key: string;
  /** The plain value, or a new secret value to store as a credential. */
  value: string;
  secret: boolean;
  source: SecretSource;
  /** The credential the plugin uses (its saved value for source "value"). */
  credentialId: string;
  credentialName: string;
  /** Credential service of the secret; empty creates a new one on save. */
  serviceId: string;
  /** The requirement key the variable is bound to (kept across edits). */
  requirementKey: string;
  /** Kept from the stored requirement: optional secrets and their purpose. */
  required: boolean;
  description: string;
}

export interface PluginSkillForm {
  uid: string;
  id?: string;
  name: string;
  description: string;
  body: string;
}

export interface PluginMcpForm {
  uid: string;
  id?: string;
  name: string;
  kind: McpKind;
  commandLine: string;
  url: string;
  transport: McpUrlTransport;
  /** Environment variables (command) or headers (URL). */
  variables: VariableRow[];
  authType: PluginMcpAuthType;
  oauthRequirementKey: string;
  /** Kept as loaded; not edited in the clients. */
  cwd: string;
  desktop: "none" | "server_start" | "first_tool";
  toolPolicy?: PluginMcpToolPolicy;
  runtimeArtifact?: { id: string; version: string } | null;
}

/** A credential requirement not bound to a variable (e.g. OAuth). */
export interface PluginRequirementForm {
  uid: string;
  reqKey: string;
  description: string;
  required: boolean;
  serviceId: string;
  credentialType: PluginServiceTypeOption;
  /** The credential the plugin uses; empty: each workspace attaches one. */
  credentialId: string;
}

export interface PluginFormModel {
  name: string;
  description: string;
  /** Only used when creating; organization plugins need an admin. */
  scope: "personal" | "organization";
  skills: PluginSkillForm[];
  mcps: PluginMcpForm[];
  requirements: PluginRequirementForm[];
  availableServices?: CredentialService[];
}

let uidCounter = 0;

/** Unique row key for list rendering (never sent to the backend). */
export function newUid(prefix = "row"): string {
  uidCounter += 1;
  return `${prefix}-${Date.now().toString(36)}-${uidCounter}`;
}

/** URL-safe slug derivation (mirrors backend `slugify` behavior loosely). */
export function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, "")
    .replace(/[\s_]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

const SECRET_NAME = /(key|token|secret|password|passwd|auth|bearer|cookie)/i;

/** Whether a variable name usually holds a secret (`GITHUB_TOKEN`). */
export function looksSecret(name: string): boolean {
  return SECRET_NAME.test(name);
}

export function emptyVariableRow(key = "", value = ""): VariableRow {
  return {
    uid: newUid("var"),
    key,
    value,
    secret: looksSecret(key),
    source: "value",
    credentialId: "",
    credentialName: "",
    serviceId: "",
    requirementKey: "",
    required: true,
    description: "",
  };
}

/** A secret with a saved value: leaving the value empty keeps it. */
export function isSavedSecret(row: VariableRow): boolean {
  return row.secret && row.source === "value" && !!row.credentialId;
}

/** Requirement key of a secret variable: `GITHUB_TOKEN` -> `github_token`. */
export function secretKey(name: string): string {
  const key = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^[_-]+|[_-]+$/g, "");
  return key || "secret";
}

export function emptySkillForm(): PluginSkillForm {
  return { uid: newUid("skill"), name: "", description: "", body: "" };
}

export function emptyMcpForm(kind: McpKind = "command"): PluginMcpForm {
  return {
    uid: newUid("mcp"),
    name: "",
    kind,
    commandLine: "",
    url: "",
    transport: "streamable_http",
    variables: [],
    authType: "none",
    oauthRequirementKey: "",
    cwd: "/workspace",
    desktop: "none",
  };
}

export function emptyRequirementForm(): PluginRequirementForm {
  return {
    uid: newUid("req"),
    reqKey: "",
    description: "",
    required: true,
    serviceId: "",
    credentialType: "env",
    credentialId: "",
  };
}

export function emptyPluginForm(
  scope: PluginFormModel["scope"] = "personal",
): PluginFormModel {
  return {
    name: "",
    description: "",
    scope,
    skills: [],
    mcps: [],
    requirements: [],
  };
}

function toServiceType(value: string): PluginServiceTypeOption {
  if (value === "file" || value === "ssh_key" || value === "mcp_oauth")
    return value;
  return "env";
}

// ---------------------------------------------------------------------------
// Command lines
// ---------------------------------------------------------------------------

/**
 * Split a command line like a POSIX shell would, without expansion:
 * whitespace separates words, quotes group them, backslash escapes.
 *
 * @throws Error on an unterminated quote.
 */
export function splitCommandLine(text: string): string[] {
  const words: string[] = [];
  let current = "";
  let inWord = false;
  let quote: "'" | '"' | null = null;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]!;
    if (quote === "'") {
      if (char === "'") quote = null;
      else current += char;
      continue;
    }
    if (quote === '"') {
      if (char === '"') quote = null;
      else if (char === "\\" && /["\\$`]/.test(text[index + 1] ?? "")) {
        current += text[index + 1];
        index += 1;
      } else current += char;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      inWord = true;
    } else if (char === "\\") {
      if (index + 1 < text.length) {
        current += text[index + 1];
        index += 1;
      }
      inWord = true;
    } else if (/\s/.test(char)) {
      if (inWord) words.push(current);
      current = "";
      inWord = false;
    } else {
      current += char;
      inWord = true;
    }
  }
  if (quote) throw new Error("Unterminated quote in the command line.");
  if (inWord) words.push(current);
  return words;
}

/** Join executable and arguments into a copyable command line. */
export function joinCommandLine(argv: string[]): string {
  return argv.map(quoteArgument).join(" ");
}

// ---------------------------------------------------------------------------
// API <-> form
// ---------------------------------------------------------------------------

function variablesFrom(
  mapping: Record<string, string> | undefined,
  requirements: Map<string, PluginCredentialRequirement>,
): VariableRow[] {
  return Object.entries(mapping ?? {}).map(([key, value]) => {
    const bound = boundCredentialKey(value);
    const requirement = bound ? requirements.get(bound) : undefined;
    if (!requirement) return { ...emptyVariableRow(key, value), secret: false };
    return {
      ...emptyVariableRow(key),
      secret: true,
      source: requirement.credential_id ? "value" : "per_user",
      credentialId: requirement.credential_id ?? "",
      credentialName: requirement.credential_name ?? "",
      serviceId: requirement.service_id,
      requirementKey: requirement.key,
      required: requirement.required,
      description: requirement.description ?? "",
    };
  });
}

/**
 * Drops credential choices the plugin may not use, e.g. personal credentials
 * after switching a new plugin to the organization. Such secrets go back to
 * "Enter a value"; other credentials go back to "each workspace adds its own".
 */
export function keepUsableCredentials(
  form: PluginFormModel,
  usable: (credentialId: string) => boolean,
): PluginFormModel {
  const allowed = (id: string) => !id || usable(id);
  return {
    ...form,
    mcps: form.mcps.map((mcp) => ({
      ...mcp,
      variables: mcp.variables.map((row) =>
        allowed(row.credentialId)
          ? row
          : {
              ...row,
              source: "value",
              value: "",
              credentialId: "",
              credentialName: "",
              serviceId: "",
            },
      ),
    })),
    requirements: form.requirements.map((requirement) =>
      allowed(requirement.credentialId)
        ? requirement
        : { ...requirement, credentialId: "" },
    ),
  };
}

/** Requirement keys bound to a variable of any server. */
function boundRequirementKeys(plugin: Plugin): Set<string> {
  const keys = new Set<string>();
  for (const server of plugin.mcp_servers ?? []) {
    const mapping = server.transport === "stdio" ? server.env : server.headers;
    for (const value of Object.values(mapping ?? {})) {
      const key = boundCredentialKey(value);
      if (key) keys.add(key);
    }
  }
  return keys;
}

/** Build an editable form from an existing plugin. */
export function pluginToForm(plugin: Plugin): PluginFormModel {
  const requirements = new Map(
    (plugin.credential_requirements ?? []).map((r) => [r.key, r]),
  );
  const bound = boundRequirementKeys(plugin);
  return {
    name: plugin.name,
    description: plugin.description ?? "",
    scope: plugin.scope === "organization" ? "organization" : "personal",
    skills: [...(plugin.skills ?? [])]
      .sort((a, b) => a.position - b.position)
      .map((skill) => ({
        uid: newUid("skill"),
        id: skill.id,
        name: skill.name,
        description: skill.description ?? "",
        body: skill.body,
      })),
    mcps: (plugin.mcp_servers ?? []).map((server) => {
      const command = server.transport === "stdio";
      return {
        uid: newUid("mcp"),
        id: server.id,
        name: server.name,
        kind: command ? "command" : "url",
        commandLine: command
          ? joinCommandLine([server.command, ...(server.args ?? [])])
          : "",
        url: command ? "" : (server.url ?? ""),
        transport: server.transport === "sse" ? "sse" : "streamable_http",
        variables: variablesFrom(
          command ? server.env : server.headers,
          requirements,
        ),
        authType: server.auth_type ?? "none",
        oauthRequirementKey: server.oauth_requirement_key ?? "",
        cwd: server.cwd || "/workspace",
        desktop: server.resources?.desktop
          ? (server.resources.desktop.activation ?? "server_start")
          : "none",
        toolPolicy: server.tool_policy,
        runtimeArtifact: server.runtime_artifact,
      };
    }),
    requirements: (plugin.credential_requirements ?? [])
      .filter((r) => !bound.has(r.key))
      .map((r) => ({
        uid: newUid("req"),
        reqKey: r.key,
        description: r.description ?? "",
        required: r.required,
        serviceId: r.service_id,
        credentialType: toServiceType(r.credential_type),
        credentialId: r.credential_id ?? "",
      })),
  };
}

type PluginFormEntries = {
  skills: PluginSkillForm;
  mcps: PluginMcpForm;
  requirements: PluginRequirementForm;
};

/** Replaces one skill, MCP server or requirement, found by its uid. */
export function updateEntry<K extends keyof PluginFormEntries>(
  form: PluginFormModel,
  list: K,
  uid: string,
  update: (entry: PluginFormEntries[K]) => PluginFormEntries[K],
): PluginFormModel {
  const entries = form[list] as PluginFormEntries[K][];
  return {
    ...form,
    [list]: entries.map((entry) => (entry.uid === uid ? update(entry) : entry)),
  };
}

/** Removes one skill, MCP server or requirement. */
export function removeEntry(
  form: PluginFormModel,
  list: keyof PluginFormEntries,
  uid: string,
): PluginFormModel {
  const entries = form[list] as Array<{ uid: string }>;
  return { ...form, [list]: entries.filter((entry) => entry.uid !== uid) };
}

/**
 * Switches an MCP server between command and URL and clears what the new
 * kind does not use, so no stale value hides in the payload.
 */
export function setMcpKind(
  form: PluginFormModel,
  uid: string,
  kind: McpKind,
): PluginFormModel {
  return updateEntry(form, "mcps", uid, (mcp) => {
    if (mcp.kind === kind) return mcp;
    return kind === "command"
      ? {
          ...mcp,
          kind,
          url: "",
          variables: [],
          authType: "none",
          oauthRequirementKey: "",
        }
      : { ...mcp, kind, desktop: "none", commandLine: "", variables: [] };
  });
}

/** Turns MCP OAuth on or off; off forgets the requirement. */
export function setMcpAuthentication(
  form: PluginFormModel,
  uid: string,
  authType: PluginMcpAuthType,
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
    credentialType: toServiceType(service.credential_type),
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
        ? { ...mcp, oauthRequirementKey: reqKey, url: service.oauth_server_url }
        : mcp,
    ),
  };
}

/**
 * Variables of one server as the backend stores them: plain values as
 * they are, secrets as `{{credential.KEY}}` of their requirement.
 */
function serverVariables(rows: VariableRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (!key) continue;
    out[key] = row.secret
      ? `{{credential.${row.requirementKey || secretKey(key)}}}`
      : row.value;
  }
  return out;
}

/** The credential requirement a secret variable stands for. */
function secretRequirement(
  row: VariableRow,
  pluginName: string,
): PluginCredentialRequirementIn {
  const name = row.key.trim();
  const service = row.serviceId
    ? { service_id: row.serviceId }
    : {
        new_service: {
          name: pluginName ? `${name} (${pluginName})` : name,
          env_var_name: name,
        },
      };
  const base = {
    key: row.requirementKey || secretKey(name),
    required: row.required,
    ...(row.description.trim() ? { description: row.description.trim() } : {}),
    ...service,
  };
  if (row.source === "per_user") return { ...base, credential_id: null };
  if (row.source === "credential")
    return { ...base, credential_id: row.credentialId || null };
  return {
    ...base,
    credential_id: row.credentialId || null,
    ...(row.value ? { value: row.value } : {}),
  };
}

function skillToIn(skill: PluginSkillForm, index: number): PluginSkillIn {
  return {
    ...(skill.id ? { id: skill.id } : {}),
    name: skill.name.trim(),
    slug: "",
    description: skill.description.trim(),
    body: skill.body,
    position: index,
  };
}

function mcpToIn(mcp: PluginMcpForm): PluginMcpServerIn {
  const base: PluginMcpServerIn = {
    ...(mcp.id ? { id: mcp.id } : {}),
    name: mcp.name.trim(),
    slug: "",
    ...(mcp.toolPolicy ? { tool_policy: mcp.toolPolicy } : {}),
    ...(mcp.runtimeArtifact ? { runtime_artifact: mcp.runtimeArtifact } : {}),
    auth_type: mcp.authType,
    oauth_requirement_key:
      mcp.authType === "oauth" ? mcp.oauthRequirementKey.trim() : "",
  };
  if (mcp.kind === "command") {
    let argv: string[] = [];
    try {
      argv = splitCommandLine(mcp.commandLine.trim());
    } catch {
      argv = [mcp.commandLine.trim()];
    }
    return {
      ...base,
      transport: "stdio",
      command: argv[0] ?? "",
      args: argv.slice(1),
      cwd: mcp.cwd || "/workspace",
      env: serverVariables(mcp.variables),
      resources:
        mcp.desktop === "none" ? {} : { desktop: { activation: mcp.desktop } },
      url: "",
      headers: {},
    };
  }
  return {
    ...base,
    transport: mcp.transport,
    command: "",
    args: [],
    env: {},
    resources: {},
    url: mcp.url.trim(),
    headers: serverVariables(mcp.variables),
  };
}

function requirementToIn(
  req: PluginRequirementForm,
): PluginCredentialRequirementIn {
  return {
    key: req.reqKey.trim(),
    description: req.description.trim(),
    required: req.required,
    service_id: req.serviceId,
    credential_id: req.credentialId || null,
  };
}

/** Every credential requirement: secret variables first, then the rest. */
function formRequirements(
  form: PluginFormModel,
): PluginCredentialRequirementIn[] {
  const byKey = new Map<string, PluginCredentialRequirementIn>();
  for (const mcp of form.mcps) {
    for (const row of mcp.variables) {
      if (!row.secret || !row.key.trim()) continue;
      const requirement = secretRequirement(row, form.name.trim());
      if (!byKey.has(requirement.key)) byKey.set(requirement.key, requirement);
    }
  }
  for (const requirement of form.requirements.map(requirementToIn)) {
    if (!byKey.has(requirement.key)) byKey.set(requirement.key, requirement);
  }
  return [...byKey.values()];
}

/** Normalize the form into a create payload. */
export function formToCreateIn(form: PluginFormModel): PluginCreateIn {
  return {
    name: form.name.trim(),
    slug: "",
    scope: form.scope,
    description: form.description.trim(),
    skills: form.skills.map(skillToIn),
    mcp_servers: form.mcps.map(mcpToIn),
    credential_requirements: formRequirements(form),
  };
}

/** Normalize the form into an update payload (lists replace fully). */
export function formToUpdateIn(form: PluginFormModel): PluginUpdateIn {
  const create = formToCreateIn(form);
  return {
    name: create.name,
    slug: create.slug,
    description: create.description,
    skills: create.skills,
    mcp_servers: create.mcp_servers,
    credential_requirements: create.credential_requirements,
  };
}

// ---------------------------------------------------------------------------
// Imports: SKILL.md and MCP configurations
// ---------------------------------------------------------------------------

/** A SKILL.md split into frontmatter name/description and body. */
export interface SkillDocument {
  name: string;
  description: string;
  body: string;
}

const FRONTMATTER = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

/** Parse a SKILL.md (frontmatter optional; mirrors the backend parser). */
export function parseSkillMarkdown(text: string): SkillDocument {
  const match = FRONTMATTER.exec(text ?? "");
  if (!match) return { name: "", description: "", body: (text ?? "").trim() };
  const values: Record<string, string> = {};
  const lines = match[1]!.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const found = /^([A-Za-z][\w-]*):[ \t]*(.*)$/.exec(lines[index]!);
    if (!found) continue;
    const key = found[1]!.toLowerCase();
    const raw = found[2]!.trim();
    if (["|", ">", "|-", ">-", "|+", ">+"].includes(raw)) {
      const block: string[] = [];
      while (
        index + 1 < lines.length &&
        (!lines[index + 1]!.trim() || /^[ \t]/.test(lines[index + 1]!))
      ) {
        index += 1;
        if (lines[index]!.trim()) block.push(lines[index]!.trim());
      }
      values[key] = block.join(raw.startsWith("|") ? "\n" : " ");
    } else {
      values[key] = raw.replace(/^(["'])(.*)\1$/, "$2").trim();
    }
  }
  return {
    name: values.name ?? "",
    description: values.description ?? "",
    body: (text ?? "").slice(match[0].length).trim(),
  };
}

/**
 * Apply pasted Markdown to a skill: frontmatter fills an empty name or
 * description and is removed from the body.
 */
export function applySkillMarkdown(
  skill: PluginSkillForm,
  text: string,
): PluginSkillForm {
  const parsed = parseSkillMarkdown(text);
  if (!FRONTMATTER.test(text ?? "")) return { ...skill, body: text };
  return {
    ...skill,
    name: skill.name.trim() ? skill.name : humanize(parsed.name),
    description: skill.description.trim()
      ? skill.description
      : parsed.description,
    body: parsed.body,
  };
}

function humanize(identifier: string): string {
  const words = identifier.replace(/[-_]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

/** A readable server name from a package, URL host or config key. */
export function serverNameFrom(source: string): string {
  const text = source.trim();
  try {
    const url = new URL(text);
    const parts = url.hostname
      .split(".")
      .filter(
        (part) =>
          !["www", "mcp", "api", "com", "io", "dev", "ai"].includes(part),
      );
    return humanize(parts[0] ?? url.hostname);
  } catch {
    // Not a URL.
  }
  let argv: string[] = [];
  try {
    argv = splitCommandLine(text);
  } catch {
    argv = text.split(/\s+/);
  }
  const runners = new Set([
    "npx",
    "uvx",
    "pnpm",
    "bunx",
    "pipx",
    "dlx",
    "run",
    "exec",
  ]);
  const target =
    argv.slice(1).find((arg) => !arg.startsWith("-") && !runners.has(arg)) ??
    argv[0] ??
    "";
  const withoutVersion = target.replace(/(.)@[^/]*$/, "$1");
  const [scope, pkg] = withoutVersion.startsWith("@")
    ? withoutVersion.slice(1).split("/")
    : ["", withoutVersion.split("/").pop() ?? ""];
  const base = (pkg ?? "")
    .replace(/^(mcp-server-|server-|mcp-)/, "")
    .replace(/(-mcp-server|-mcp|-server)$/, "");
  const chosen = !base || base === "mcp" ? (scope ?? "") : base;
  return humanize(chosen || target);
}

/** Values a pasted config expects the user to fill in, e.g. `${TOKEN}`. */
const PLACEHOLDER_VALUE = /\$\{[^}]*\}|<[^>]+>|^your[_-]|^x{3,}$|^\.\.\.$/i;

function variablesFromConfig(values: unknown): VariableRow[] {
  if (!values || typeof values !== "object") return [];
  return Object.entries(values as Record<string, unknown>).map(([key, raw]) => {
    const value = typeof raw === "string" ? raw : String(raw ?? "");
    const placeholder = PLACEHOLDER_VALUE.test(value.trim());
    return {
      ...emptyVariableRow(key, placeholder ? "" : value),
      secret: looksSecret(key) || placeholder,
    };
  });
}

function serverFromConfig(
  name: string,
  config: Record<string, unknown>,
): PluginMcpForm {
  const type = String(config.type ?? config.transport ?? "").toLowerCase();
  const url = typeof config.url === "string" ? config.url : "";
  const server = emptyMcpForm(url ? "url" : "command");
  server.name = name ? humanize(name) : "";
  if (url) {
    server.url = url;
    server.transport =
      type === "sse" || /\/sse\/?$/.test(url) ? "sse" : "streamable_http";
    server.variables = variablesFromConfig(
      config.headers ?? config.http_headers,
    );
    if (!server.name) server.name = serverNameFrom(url);
    return server;
  }
  const command = typeof config.command === "string" ? config.command : "";
  const args = Array.isArray(config.args) ? config.args.map(String) : [];
  server.commandLine = args.length
    ? joinCommandLine([command, ...args])
    : command;
  server.variables = variablesFromConfig(config.env);
  if (!server.name) server.name = serverNameFrom(server.commandLine);
  return server;
}

/** Result of reading a pasted MCP configuration. */
export interface McpImport {
  servers: PluginMcpForm[];
  error?: string;
}

/**
 * Read MCP servers from pasted text: `{"mcpServers": {...}}` (Claude,
 * Cursor), `{"servers": {...}}` (VS Code), a map of named servers, a single
 * server object, a URL or a command line such as `npx -y <package>`.
 */
export function parseMcpConfig(text: string): McpImport {
  const source = (text ?? "").trim();
  if (!source) return { servers: [], error: "Paste a configuration first." };
  if (source.startsWith("{") || source.startsWith("[")) {
    let data: unknown;
    try {
      data = JSON.parse(source);
    } catch {
      return { servers: [], error: "This is not valid JSON." };
    }
    const record = (data && typeof data === "object" ? data : {}) as Record<
      string,
      unknown
    >;
    const isServer = (value: unknown) =>
      !!value &&
      typeof value === "object" &&
      ("command" in (value as object) || "url" in (value as object));
    let entries: Array<[string, unknown]>;
    if (isServer(record)) entries = [["", record]];
    else {
      const map = (record.mcpServers ??
        record.servers ??
        record.mcp_servers ??
        record) as Record<string, unknown>;
      entries = Object.entries(map ?? {}).filter(([, value]) =>
        isServer(value),
      );
    }
    if (!entries.length)
      return {
        servers: [],
        error: "No MCP server found. Expected `command` or `url` entries.",
      };
    return {
      servers: entries.map(([name, value]) =>
        serverFromConfig(name, value as Record<string, unknown>),
      ),
    };
  }
  if (/^https?:\/\//i.test(source)) {
    return { servers: [serverFromConfig("", { url: source })] };
  }
  const line = source.replace(/^(claude|codex)\s+mcp\s+add\b.*?\s--\s+/, "");
  try {
    splitCommandLine(line);
  } catch (cause) {
    return { servers: [], error: (cause as Error).message };
  }
  const server = emptyMcpForm("command");
  server.commandLine = line;
  server.name = serverNameFrom(line);
  return { servers: [server] };
}

// ---------------------------------------------------------------------------
// Validation (mirrors backend/apps/plugins/services.py)
// ---------------------------------------------------------------------------

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const HEADER_NAME = /^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$/;
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const KEY_RE = /^[a-z0-9_]+(?:[a-z0-9_-]*[a-z0-9_])?$/i;
const SUPPORTED_PLACEHOLDER =
  /\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}/g;

function isDerivableSlug(name: string): boolean {
  const slug = slugify(name);
  return !!slug && slug.length <= 255 && SLUG_RE.test(slug);
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

/** Errors that belong to no skill, MCP server or requirement. */
export function generalErrors(errors: string[]): string[] {
  return errors.filter(
    (error) => !/^(Skill #\d+|MCP server [#"]|Requirement [#"])/.test(error),
  );
}

/** Extract `credential.KEY` references from a value. */
export function referencedPlaceholderKeys(value: string): string[] {
  return [...value.matchAll(SUPPORTED_PLACEHOLDER)].map((match) => match[1]!);
}

function validateCommand(mcp: PluginMcpForm, label: string): string[] {
  const line = mcp.commandLine.trim();
  if (!line) return [`${label}: enter the command that starts the server.`];
  let argv: string[];
  try {
    argv = splitCommandLine(line);
  } catch (cause) {
    return [`${label}: ${(cause as Error).message}`];
  }
  const executable = argv[0] ?? "";
  if (!executable || /[;|&$`]/.test(executable))
    return [
      `${label}: the command must start with a program, not shell syntax.`,
    ];
  if (argv.length > 65 || argv.some((arg) => arg.length > 1024))
    return [`${label}: the command is too long.`];
  return [];
}

function validateUrl(mcp: PluginMcpForm, label: string): string[] {
  const url = mcp.url.trim();
  if (!/^https?:\/\//i.test(url)) return [`${label}: enter an http(s) URL.`];
  try {
    const parsed = new URL(url);
    if (parsed.username || parsed.password || parsed.hash)
      return [`${label}: the URL must not contain a user name or #fragment.`];
  } catch {
    return [`${label}: the URL is not valid.`];
  }
  return url.length > 2048 ? [`${label}: the URL is too long.`] : [];
}

function validateVariables(
  mcp: PluginMcpForm,
  label: string,
  requirementKeys: Set<string>,
): string[] {
  const errors: string[] = [];
  const command = mcp.kind === "command";
  const field = command ? "variable" : "header";
  const seen = new Set<string>();
  for (const row of mcp.variables) {
    const key = row.key.trim();
    if (!key) {
      if (row.value.trim()) errors.push(`${label}: a ${field} needs a name.`);
      continue;
    }
    if (!(command ? ENV_NAME : HEADER_NAME).test(key) || key.length > 256)
      errors.push(`${label}: "${key}" is not a valid ${field} name.`);
    const folded = command ? key : key.toLowerCase();
    if (seen.has(folded)) errors.push(`${label}: "${key}" is set twice.`);
    seen.add(folded);
    if (row.value.length > 4096)
      errors.push(`${label}: the value of "${key}" is too long.`);
    if (row.secret) {
      if (row.source === "value" && !row.value && !row.credentialId)
        errors.push(`${label}: enter the secret value of "${key}".`);
      if (row.source === "credential" && !row.credentialId)
        errors.push(`${label}: choose the credential for "${key}".`);
      continue;
    }
    for (const ref of referencedPlaceholderKeys(row.value)) {
      if (!requirementKeys.has(ref))
        errors.push(
          `${label}: "${key}" uses unknown credential "${ref}"; add it under Advanced.`,
        );
    }
    const stripped = row.value.replace(SUPPORTED_PLACEHOLDER, "");
    if (/\{\{|\}\}/.test(stripped))
      errors.push(
        `${label}: "${key}" contains an unsupported placeholder (only ${PLACEHOLDER_EXAMPLE}).`,
      );
    if (
      command &&
      ["DISPLAY", "XAUTHORITY"].includes(key) &&
      mcp.desktop !== "none"
    )
      errors.push(`${label}: the managed desktop sets ${key} itself.`);
  }
  if (
    mcp.authType === "oauth" &&
    mcp.variables.some(
      (row) => row.key.trim().toLowerCase() === "authorization",
    )
  )
    errors.push(`${label}: sign-in manages the Authorization header.`);
  return errors;
}

/** Client-side validation of the whole form. */
export function validatePluginForm(form: PluginFormModel): string[] {
  const errors: string[] = [];
  const name = form.name.trim();
  if (!name) errors.push("Enter a plugin name.");
  else if (name.length > 255) errors.push("The plugin name is too long.");
  else if (!isDerivableSlug(name))
    errors.push("The plugin name needs letters or digits.");
  if (form.description.trim().length > 10000)
    errors.push("The plugin description is too long.");

  form.skills.forEach((skill, index) => {
    const label = skillLabel(index);
    const parsed = parseSkillMarkdown(skill.body);
    const skillName = skill.name.trim() || parsed.name;
    if (!skillName) errors.push(`${label}: enter a name.`);
    else if (!isDerivableSlug(skillName))
      errors.push(`${label}: the name needs letters or digits.`);
    if (!parsed.body.trim()) errors.push(`${label}: write its instructions.`);
    else if (skill.body.length > 200000)
      errors.push(`${label}: the instructions are too long.`);
    if (skill.description.trim().length > 1024)
      errors.push(`${label}: the description must be at most 1024 characters.`);
  });

  const requirementKeys = new Set(
    form.requirements.map((r) => r.reqKey.trim()).filter(Boolean),
  );
  const oauthKeys = new Set(
    form.requirements
      .filter((r) => r.credentialType === "mcp_oauth" && r.required)
      .map((r) => r.reqKey.trim()),
  );
  const serverNames = new Set<string>();
  form.mcps.forEach((mcp, index) => {
    const label = mcpLabel(mcp, index);
    const serverName = mcp.name.trim();
    if (!serverName) errors.push(`${label}: enter a name.`);
    else if (!isDerivableSlug(serverName))
      errors.push(`${label}: the name needs letters or digits.`);
    else if (serverNames.has(slugify(serverName)))
      errors.push(`${label}: another MCP server has the same name.`);
    serverNames.add(slugify(serverName));
    errors.push(
      ...(mcp.kind === "command"
        ? validateCommand(mcp, label)
        : validateUrl(mcp, label)),
    );
    errors.push(...validateVariables(mcp, label, requirementKeys));
    if (mcp.authType === "oauth") {
      if (mcp.kind !== "url")
        errors.push(`${label}: sign-in is only available for URL servers.`);
      if (!oauthKeys.has(mcp.oauthRequirementKey.trim()))
        errors.push(`${label}: choose the OAuth credential to sign in with.`);
    }
  });

  const seenKeys = new Set<string>();
  const seenServices = new Set<string>();
  form.requirements.forEach((req, index) => {
    const key = req.reqKey.trim();
    const label = requirementLabel(req, index);
    if (!key) errors.push(`${label}: enter a key.`);
    else if (key.length > 255 || !KEY_RE.test(key))
      errors.push(`${label}: use letters, digits, dashes or underscores.`);
    else if (seenKeys.has(key)) errors.push(`${label}: the key is used twice.`);
    else seenKeys.add(key);
    if (req.description.trim().length > 2000)
      errors.push(`${label}: the description is too long.`);
    if (!req.serviceId) {
      errors.push(`${label}: choose a credential service.`);
      return;
    }
    if (seenServices.has(req.serviceId))
      errors.push(`${label}: the credential service is used twice.`);
    seenServices.add(req.serviceId);
    const service = form.availableServices?.find(
      (entry) => entry.id === req.serviceId,
    );
    if (form.availableServices && !service)
      errors.push(`${label}: the credential service is not available here.`);
    if (req.credentialType === "mcp_oauth") {
      if (!req.required)
        errors.push(`${label}: OAuth credentials must be required.`);
      const servers = form.mcps.filter(
        (mcp) =>
          mcp.authType === "oauth" && mcp.oauthRequirementKey.trim() === key,
      );
      if (servers.length !== 1)
        errors.push(`${label}: use it for exactly one signed-in MCP server.`);
      for (const server of servers) {
        if (
          service?.oauth_server_url &&
          server.url.trim() !== service.oauth_server_url
        )
          errors.push(
            `${label}: the server URL must be ${service.oauth_server_url}.`,
          );
      }
    }
  });
  return errors;
}
