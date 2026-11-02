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
}

export interface WorkspacePluginInput {
  id: string;
  enabled: boolean;
  published: boolean;
  org_enabled: boolean;
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

/** Plugins offered for workspaces of the given organization. */
export function workspacePluginCatalog<P extends WorkspacePluginInput>(
  plugins: P[],
  organizationId: string | null,
): P[] {
  return plugins.filter(
    (plugin) =>
      plugin.enabled &&
      plugin.published &&
      plugin.org_enabled &&
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
