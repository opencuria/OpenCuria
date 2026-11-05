import { ApiRequestError, type ApiClient } from "./api";
import type {
  ApiKey,
  ApiKeyCreateInput,
  ApiKeyCreated,
  ApiKeyPermission,
  CapturedImage,
  ChatGptOAuthStart,
  ChatGptOAuthStartInput,
  ChatGptOAuthStatus,
  ComputerUseConfig,
  Credential,
  CredentialCreateInput,
  CredentialPublicKey,
  CredentialService,
  CredentialServiceCreateInput,
  CredentialUpdateInput,
  DeletionGraph,
  DeletionRequest,
  DeletionTarget,
  ImageDefinition,
  ImageDefinitionInput,
  OperationDisposition,
  OperationInspection,
  Organization,
  Plugin,
  PluginCreateIn,
  PluginUpdateIn,
  ProviderAccount,
  ProviderAccountInput,
  ProviderAccountProvider,
  RunnerCreateInput,
  RunnerCreateResult,
  RunnerImageBuild,
  Runner,
  RunnerStorage,
  RunnerSystemMetrics,
  RunnerUpdateInput,
  SubagentConfig,
  VoiceSettings,
  WorkspacePolicyInput,
} from "./types";

function segment(value: string): string {
  return encodeURIComponent(value);
}

function errorDetail(data: unknown, fallback: string): [string, string] {
  const payload =
    data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  return [
    typeof payload.detail === "string" ? payload.detail : fallback,
    typeof payload.code === "string" ? payload.code : "error",
  ];
}

/** Organization profile, creation and workspace policy (admins write). */
export function createOrganizationsApi(getApi: () => ApiClient) {
  return {
    get: (organizationId: string, signal?: AbortSignal) =>
      getApi().get<Organization>(
        `/organizations/${segment(organizationId)}/`,
        signal,
      ),
    create: (name: string, signal?: AbortSignal) =>
      getApi().post<Organization>("/organizations/", { name }, signal),
    updateWorkspacePolicy: (
      organizationId: string,
      input: WorkspacePolicyInput,
      signal?: AbortSignal,
    ) =>
      getApi().patch<Organization>(
        `/organizations/${segment(organizationId)}/workspace-policy/`,
        input,
        signal,
      ),
  };
}

/** Named provider accounts, the active selection and ChatGPT device login. */
export function createProviderAccountsApi(getApi: () => ApiClient) {
  const accountPath = (provider: ProviderAccountProvider, id?: string) =>
    `/provider-config/accounts/${segment(provider)}/${id ? `${segment(id)}/` : ""}`;
  return {
    list: (signal?: AbortSignal) =>
      getApi().get<ProviderAccount[]>("/provider-config/accounts/", signal),
    create: (
      provider: ProviderAccountProvider,
      input: ProviderAccountInput,
      signal?: AbortSignal,
    ) => getApi().post<ProviderAccount>(accountPath(provider), input, signal),
    update: (
      provider: ProviderAccountProvider,
      accountId: string,
      input: ProviderAccountInput,
      signal?: AbortSignal,
    ) =>
      getApi().put<ProviderAccount>(
        accountPath(provider, accountId),
        input,
        signal,
      ),
    delete: (
      provider: ProviderAccountProvider,
      accountId: string,
      signal?: AbortSignal,
    ) => getApi().delete<void>(accountPath(provider, accountId), signal),
    /** Use this account for new responses, or explicitly none (`null`). */
    select: (
      provider: ProviderAccountProvider,
      accountId: string | null,
      signal?: AbortSignal,
    ) =>
      getApi().put<void>(
        `/provider-config/selection/${segment(provider)}/`,
        { account_id: accountId },
        signal,
      ),
    startChatGptOAuth: (input: ChatGptOAuthStartInput, signal?: AbortSignal) =>
      getApi().post<ChatGptOAuthStart>(
        "/provider-config/providers/chatgpt/oauth/start/",
        input,
        signal,
      ),
    /** Maps an expired (410) or unknown (404) flow to a status instead of throwing. */
    async chatGptOAuthStatus(
      flowId: string,
      signal?: AbortSignal,
    ): Promise<ChatGptOAuthStatus> {
      const result = await getApi().requestWithStatus<ChatGptOAuthStatus>(
        "GET",
        `/provider-config/providers/chatgpt/oauth/status/?flow_id=${segment(flowId)}`,
        undefined,
        signal,
      );
      const accountId = result.data?.account_id ?? "";
      if (result.status === 404)
        return { status: "no_flow", account_id: accountId };
      if (result.status === 410)
        return {
          status: result.data?.status === "denied" ? "denied" : "expired",
          account_id: accountId,
        };
      if (!result.ok) {
        const [detail, code] = errorDetail(
          result.data,
          "OAuth status request failed",
        );
        throw new ApiRequestError(result.status, detail, code, result.data);
      }
      return result.data;
    },
    cancelChatGptOAuth: (flowId: string, signal?: AbortSignal) =>
      getApi().post<void>(
        "/provider-config/providers/chatgpt/oauth/cancel/",
        { flow_id: flowId },
        signal,
      ),
  };
}

/** Organization-wide agent limits and the voice model catalog. */
export function createAgentSettingsApi(getApi: () => ApiClient) {
  return {
    getSubagentConfig: (signal?: AbortSignal) =>
      getApi().get<SubagentConfig>("/subagent-config/", signal),
    saveSubagentConfig: (input: SubagentConfig, signal?: AbortSignal) =>
      getApi().put<SubagentConfig>("/subagent-config/", input, signal),
    getComputerUseConfig: (signal?: AbortSignal) =>
      getApi().get<ComputerUseConfig>("/computer-use-config/", signal),
    /** Partial updates are merged over the stored values. */
    saveComputerUseConfig: (
      input: Partial<ComputerUseConfig>,
      signal?: AbortSignal,
    ) =>
      getApi().put<ComputerUseConfig>("/computer-use-config/", input, signal),
    getVoiceSettings: (signal?: AbortSignal) =>
      getApi().get<VoiceSettings>("/voice/config/", signal),
  };
}

/** Credentials, their services and OAuth grants. */
export function createCredentialsApi(getApi: () => ApiClient) {
  return {
    list: (signal?: AbortSignal) =>
      getApi().get<Credential[]>("/credentials/", signal),
    create: (input: CredentialCreateInput, signal?: AbortSignal) =>
      getApi().post<Credential>("/credentials/", input, signal),
    update: (
      credentialId: string,
      input: CredentialUpdateInput,
      signal?: AbortSignal,
    ) =>
      getApi().patch<Credential>(
        `/credentials/${segment(credentialId)}/`,
        input,
        signal,
      ),
    delete: (credentialId: string, signal?: AbortSignal) =>
      getApi().delete<void>(`/credentials/${segment(credentialId)}/`, signal),
    publicKey: (credentialId: string, signal?: AbortSignal) =>
      getApi().get<CredentialPublicKey>(
        `/credentials/${segment(credentialId)}/public-key/`,
        signal,
      ),
    /** Removes the OAuth grant; workspace attachments are kept. */
    disconnectOAuth: (credentialId: string, signal?: AbortSignal) =>
      getApi().delete<void>(
        `/credentials/${segment(credentialId)}/oauth/disconnect/`,
        signal,
      ),
    /** Services the active organization can create credentials for. */
    listServices: (signal?: AbortSignal) =>
      getApi().get<CredentialService[]>("/credential-services/", signal),
  };
}

/** Organization-owned credential services and activation (admins only). */
export function createCredentialServicesApi(getApi: () => ApiClient) {
  return {
    list: (signal?: AbortSignal) =>
      getApi().get<CredentialService[]>("/org-credential-services/", signal),
    create: (input: CredentialServiceCreateInput, signal?: AbortSignal) =>
      getApi().post<CredentialService>(
        "/org-credential-services/",
        input,
        signal,
      ),
    setActive: (serviceId: string, active: boolean, signal?: AbortSignal) =>
      getApi().post<CredentialService>(
        `/org-credential-services/${segment(serviceId)}/activation/`,
        { active },
        signal,
      ),
  };
}

/** The plugin catalog; edits and activation are for organization admins. */
export function createPluginsApi(getApi: () => ApiClient) {
  return {
    list: (signal?: AbortSignal) => getApi().get<Plugin[]>("/plugins/", signal),
    create: (input: PluginCreateIn, signal?: AbortSignal) =>
      getApi().post<Plugin>("/plugins/", input, signal),
    update: (pluginId: string, input: PluginUpdateIn, signal?: AbortSignal) =>
      getApi().patch<Plugin>(`/plugins/${segment(pluginId)}/`, input, signal),
    delete: (pluginId: string, signal?: AbortSignal) =>
      getApi().delete<void>(`/plugins/${segment(pluginId)}/`, signal),
    setActive: (pluginId: string, active: boolean, signal?: AbortSignal) =>
      getApi().post<Plugin>(
        `/plugins/${segment(pluginId)}/activation/`,
        { active },
        signal,
      ),
  };
}

/** The signed-in user's API keys (JWT sessions only). */
export function createApiKeysApi(getApi: () => ApiClient) {
  return {
    list: (signal?: AbortSignal) =>
      getApi().get<ApiKey[]>("/auth/api-keys/", signal),
    create: (input: ApiKeyCreateInput, signal?: AbortSignal) =>
      getApi().post<ApiKeyCreated>("/auth/api-keys/", input, signal),
    updatePermissions: (
      keyId: string,
      permissions: string[],
      signal?: AbortSignal,
    ) =>
      getApi().patch<ApiKey>(
        `/auth/api-keys/${segment(keyId)}/`,
        { permissions },
        signal,
      ),
    /** Revocation is permanent; the key stays listed as revoked. */
    revoke: (keyId: string, signal?: AbortSignal) =>
      getApi().delete<void>(`/auth/api-keys/${segment(keyId)}/`, signal),
    permissions: (signal?: AbortSignal) =>
      getApi().get<ApiKeyPermission[]>("/auth/api-key-permissions/", signal),
  };
}

/** Captured images, image definitions and their builds on runners. */
export function createImagesApi(getApi: () => ApiClient) {
  const builds = (definitionId: string, runnerId?: string) =>
    `/image-definitions/${segment(definitionId)}/runner-builds/${runnerId ? `${segment(runnerId)}/` : ""}`;
  return {
    renameCaptured: (imageId: string, name: string, signal?: AbortSignal) =>
      getApi().patch<CapturedImage>(
        `/captured-images/${segment(imageId)}/`,
        { name },
        signal,
      ),
    createDefinition: (input: ImageDefinitionInput, signal?: AbortSignal) =>
      getApi().post<ImageDefinition>("/image-definitions/", input, signal),
    updateDefinition: (
      definitionId: string,
      input: ImageDefinitionInput,
      signal?: AbortSignal,
    ) =>
      getApi().patch<ImageDefinition>(
        `/image-definitions/${segment(definitionId)}/`,
        input,
        signal,
      ),
    /** The server picks a unique copy name when none is given. */
    duplicateDefinition: (
      definitionId: string,
      name?: string,
      signal?: AbortSignal,
    ) =>
      getApi().post<ImageDefinition>(
        `/image-definitions/${segment(definitionId)}/duplicate/`,
        name ? { name } : {},
        signal,
      ),
    /** Restores a definition whose deletion failed. */
    activateDefinition: (definitionId: string, signal?: AbortSignal) =>
      getApi().post<ImageDefinition>(
        `/image-definitions/${segment(definitionId)}/activate/`,
        undefined,
        signal,
      ),
    /** Build (or retry) the definition on a runner. */
    createRunnerBuild: (
      definitionId: string,
      input: { runner_id: string; activate?: boolean; message?: string },
      signal?: AbortSignal,
    ) => getApi().post<RunnerImageBuild>(builds(definitionId), input, signal),
    updateRunnerBuild: (
      definitionId: string,
      runnerId: string,
      input: {
        action: "activate" | "deactivate" | "rebuild";
        message?: string;
      },
      signal?: AbortSignal,
    ) =>
      getApi().patch<RunnerImageBuild>(
        builds(definitionId, runnerId),
        input,
        signal,
      ),
    runnerBuildLog: (
      definitionId: string,
      runnerId: string,
      signal?: AbortSignal,
    ) =>
      getApi().get<{ build_log: string }>(
        `${builds(definitionId, runnerId)}log/`,
        signal,
      ),
  };
}

/** Runner registration, limits, metrics, storage inventory and operations. */
export function createRunnersApi(getApi: () => ApiClient) {
  const runner = (id: string) => `/runners/${segment(id)}/`;
  return {
    list: (signal?: AbortSignal) => getApi().get<Runner[]>("/runners/", signal),
    create: (input: RunnerCreateInput, signal?: AbortSignal) =>
      getApi().post<RunnerCreateResult>("/runners/", input, signal),
    update: (
      runnerId: string,
      input: RunnerUpdateInput,
      signal?: AbortSignal,
    ) => getApi().patch<Runner>(runner(runnerId), input, signal),
    /** Resolves `null` while the runner has not reported metrics yet. */
    async latestMetrics(
      runnerId: string,
      signal?: AbortSignal,
    ): Promise<RunnerSystemMetrics | null> {
      try {
        return await getApi().get<RunnerSystemMetrics>(
          `${runner(runnerId)}metrics/latest/`,
          signal,
        );
      } catch (error) {
        if (error instanceof ApiRequestError && error.status === 404)
          return null;
        throw error;
      }
    },
    storage: (runnerId: string, signal?: AbortSignal) =>
      getApi().get<RunnerStorage>(`${runner(runnerId)}storage/`, signal),
    refreshStorage: (runnerId: string, signal?: AbortSignal) =>
      getApi().post<{ requested_at: string }>(
        `${runner(runnerId)}storage/refresh/`,
        undefined,
        signal,
      ),
    inspectOperation: (operationId: string, signal?: AbortSignal) =>
      getApi().get<OperationInspection>(
        `/runners/operations/${segment(operationId)}/`,
        signal,
      ),
    disposeOperation: (
      operationId: string,
      action: OperationDisposition,
      signal?: AbortSignal,
    ) =>
      getApi().post<unknown>(
        `/runners/operations/${segment(operationId)}/${segment(action)}/`,
        undefined,
        signal,
      ),
  };
}

/** Deferred or forced removal of images, builds and definitions. */
export function createDeletionsApi(getApi: () => ApiClient) {
  const base = "/image-artifacts/deletions/";
  return {
    list: (signal?: AbortSignal) =>
      getApi().get<DeletionRequest[]>(base, signal),
    /** Everything a forced deletion would remove (admins only). */
    preview: (target: DeletionTarget, signal?: AbortSignal) =>
      getApi().post<DeletionGraph>(`${base}preview/`, target, signal),
    request: (
      target: DeletionTarget,
      mode: "deferred" | "force" = "deferred",
      fingerprint = "",
      signal?: AbortSignal,
    ) =>
      getApi().post<DeletionRequest>(
        base,
        { ...target, mode, fingerprint },
        signal,
      ),
    cancel: (requestId: string, signal?: AbortSignal) =>
      getApi().post<DeletionRequest>(
        `${base}${segment(requestId)}/cancel/`,
        undefined,
        signal,
      ),
  };
}
