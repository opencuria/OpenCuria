/**
 * Plugin and credential selection rules for creating or editing a workspace,
 * shared by the web and native clients.
 */

export interface WorkspaceCredentialInput {
  id: string;
  service_id: string;
  credential_type: string;
  oauth_connected: boolean;
}

export interface PluginCredentialRequirementInput {
  id: string;
  key: string;
  required: boolean;
  service_id: string;
  service_name?: string;
  /** The plugin brings this credential itself; nothing to attach. */
  credential_id?: string | null;
}

export interface WorkspacePluginInput {
  id: string;
  name?: string;
  /** Personal plugins, and shared ones released for the organization. */
  available: boolean;
  organization_id: string | null;
  credential_requirements: PluginCredentialRequirementInput[];
}

export interface WorkspacePluginSelection<
  P extends WorkspacePluginInput,
  C extends WorkspaceCredentialInput,
> {
  /** Plugins a workspace in this organization may select. */
  catalog: P[];
  /** Selected credentials that still exist. */
  selectedCredentials: C[];
  /** Selected plugins that are no longer offered; they block saving. */
  unavailablePluginIds: string[];
  /** Selected credentials that no longer exist; they block saving. */
  unavailableCredentialIds: string[];
  /** Services with more than one selected credential; they block saving. */
  duplicateServiceIds: string[];
  /** Required plugin credentials without a connected selection. */
  missingRequired: Array<{
    plugin: P;
    requirement: P["credential_requirements"][number];
  }>;
  /** Whether the selection can be saved. */
  valid: boolean;
}

/** OAuth credentials only count once connected; other credential kinds always do. */
export function isCredentialConnected(
  credential: Pick<
    WorkspaceCredentialInput,
    "credential_type" | "oauth_connected"
  >,
): boolean {
  return (
    credential.credential_type !== "mcp_oauth" || credential.oauth_connected
  );
}

/** Toggle a credential, keeping at most one selected credential per service. */
export function toggleWorkspaceCredentialSelection(
  selectedIds: string[],
  nextCredential: Pick<WorkspaceCredentialInput, "id" | "service_id">,
  allCredentials: Array<Pick<WorkspaceCredentialInput, "id" | "service_id">>,
): string[] {
  if (selectedIds.includes(nextCredential.id)) {
    return selectedIds.filter((id) => id !== nextCredential.id);
  }

  const credentialsById = new Map(
    allCredentials.map((credential) => [credential.id, credential]),
  );
  const withoutSameService = selectedIds.filter(
    (id) => credentialsById.get(id)?.service_id !== nextCredential.service_id,
  );

  return [...withoutSameService, nextCredential.id];
}

/**
 * Credential selection after enabling a plugin: every requirement the plugin
 * does not bring itself and the selection does not cover yet gets the only
 * connected credential of its service, when there is exactly one.
 */
export function autoAttachCredentials(
  plugin: Pick<WorkspacePluginInput, "credential_requirements">,
  credentialIds: string[],
  credentials: WorkspaceCredentialInput[],
): string[] {
  let next = credentialIds;
  for (const requirement of plugin.credential_requirements) {
    if (requirement.credential_id) continue;
    const ofService = credentials.filter(
      (credential) => credential.service_id === requirement.service_id,
    );
    if (ofService.some((credential) => next.includes(credential.id))) continue;
    const connected = ofService.filter(isCredentialConnected);
    if (connected.length === 1) next = [...next, connected[0]!.id];
  }
  return next;
}

/** Plugins offered for workspaces of the given organization. */
export function workspacePluginCatalog<P extends WorkspacePluginInput>(
  plugins: P[],
  organizationId: string | null,
): P[] {
  return plugins.filter(
    (plugin) =>
      plugin.available &&
      (plugin.organization_id === null ||
        plugin.organization_id === organizationId),
  );
}

/** Evaluate a plugin/credential selection against the current catalog. */
export function evaluateWorkspacePluginSelection<
  P extends WorkspacePluginInput,
  C extends WorkspaceCredentialInput,
>(input: {
  plugins: P[];
  credentials: C[];
  organizationId: string | null;
  pluginIds: string[];
  credentialIds: string[];
}): WorkspacePluginSelection<P, C> {
  const catalog = workspacePluginCatalog(input.plugins, input.organizationId);
  const selectedCredentials = input.credentials.filter((credential) =>
    input.credentialIds.includes(credential.id),
  );
  const unavailablePluginIds = input.pluginIds.filter(
    (id) => !catalog.some((plugin) => plugin.id === id),
  );
  const unavailableCredentialIds = input.credentialIds.filter(
    (id) => !input.credentials.some((credential) => credential.id === id),
  );
  const counts = new Map<string, number>();
  for (const credential of selectedCredentials) {
    counts.set(
      credential.service_id,
      (counts.get(credential.service_id) ?? 0) + 1,
    );
  }
  const duplicateServiceIds = [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([serviceId]) => serviceId);
  const missingRequired = catalog
    .filter((plugin) => input.pluginIds.includes(plugin.id))
    .flatMap((plugin) =>
      plugin.credential_requirements.map((requirement) => ({
        plugin,
        requirement,
      })),
    )
    .filter(
      ({ requirement }) =>
        requirement.required &&
        !requirement.credential_id &&
        !selectedCredentials.some(
          (credential) =>
            credential.service_id === requirement.service_id &&
            isCredentialConnected(credential),
        ),
    );
  return {
    catalog,
    selectedCredentials,
    unavailablePluginIds,
    unavailableCredentialIds,
    duplicateServiceIds,
    missingRequired,
    valid:
      !unavailablePluginIds.length &&
      !unavailableCredentialIds.length &&
      !duplicateServiceIds.length &&
      !missingRequired.length,
  };
}

/** Selected plugins that take the credential from the workspace, e.g. "Used by GitHub tools". */
export function pluginsUsingCredential<P extends WorkspacePluginInput>(
  catalog: P[],
  pluginIds: string[],
  credential: Pick<WorkspaceCredentialInput, "service_id">,
): P[] {
  return catalog.filter(
    (plugin) =>
      pluginIds.includes(plugin.id) &&
      plugin.credential_requirements.some(
        (requirement) =>
          !requirement.credential_id &&
          requirement.service_id === credential.service_id,
      ),
  );
}

export interface WorkspaceSelectionSummary {
  /** e.g. "GitHub tools, Notion · 2 credentials", or "None". */
  text: string;
  /** What blocks saving, e.g. "Missing GitHub credential"; null when it can be saved. */
  issue: string | null;
}

function countLabel(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** One line for the plugins and credentials of a workspace and what blocks saving them. */
export function workspaceSelectionSummary<
  P extends WorkspacePluginInput,
  C extends WorkspaceCredentialInput,
>(
  selection: WorkspacePluginSelection<P, C>,
  pluginIds: string[],
  credentialIds: string[],
): WorkspaceSelectionSummary {
  const names = selection.catalog
    .filter((plugin) => pluginIds.includes(plugin.id))
    .map((plugin) => plugin.name || "Plugin");
  const hidden =
    Math.max(names.length - 2, 0) + selection.unavailablePluginIds.length;
  const plugins = names.length
    ? `${names.slice(0, 2).join(", ")}${hidden > 0 ? ` +${hidden}` : ""}`
    : pluginIds.length
      ? countLabel(pluginIds.length, "plugin")
      : null;
  const credentials = credentialIds.length
    ? countLabel(credentialIds.length, "credential")
    : null;
  const services = [
    ...new Set(
      selection.missingRequired.map(
        ({ requirement }) => requirement.service_name || requirement.key,
      ),
    ),
  ];
  const issue =
    services.length === 1
      ? `Missing ${services[0]} credential`
      : services.length > 1
        ? `Missing ${services.length} credentials`
        : selection.unavailablePluginIds.length ||
            selection.unavailableCredentialIds.length
          ? "Remove unavailable items"
          : selection.duplicateServiceIds.length
            ? "One credential per service"
            : null;
  return {
    text: [plugins, credentials].filter(Boolean).join(" · ") || "None",
    issue,
  };
}
