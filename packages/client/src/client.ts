import { ApiClient, type ApiClientOptions, type FetchLike } from "./api";
import { createHarnessApi } from "./harness";
import {
  RealtimeClient,
  type RealtimeOptions,
  type SocketFactory,
} from "./realtime";
import type {
  AgentActivitySnapshot,
  MobileDevice,
  MobileDeviceRegistration,
  PushCapabilities,
  AuthUser,
  HarnessConversation,
  Skill,
  SkillCreateInput,
  SkillUpdateInput,
  TokenPair,
  Workspace,
} from "./types";

export interface CreateClientOptions {
  apiBaseUrl: string;
  wsBaseUrl: string;
  getAccessToken: () => string | null;
  getRefreshToken: () => string | null;
  setTokens: (tokens: TokenPair) => Promise<void> | void;
  getOrganizationId: () => string | null;
  onAuthenticationExpired: () => void;
  fetch?: FetchLike;
  socketFactory?: SocketFactory;
  fileTransferTimeoutMs?: number;
  createFileRequestId?: () => string;
}

export interface ClientApi extends ApiClient {
  readonly baseUrl: string;
}

export interface OpenCuriaClient {
  api: ClientApi;
  notifications: {
    capabilities(signal?: AbortSignal): Promise<PushCapabilities>;
    registerDevice(
      id: string,
      input: MobileDeviceRegistration,
      signal?: AbortSignal,
    ): Promise<MobileDevice>;
    unregisterDevice(id: string, signal?: AbortSignal): Promise<void>;
    registerActivity(
      id: string,
      activityId: string,
      pushToken: string,
      signal?: AbortSignal,
    ): Promise<void>;
    activity(id: string, signal?: AbortSignal): Promise<AgentActivitySnapshot>;
  };
  auth: {
    login(email: string, password: string): Promise<TokenPair>;
    me(signal?: AbortSignal): Promise<AuthUser>;
  };
  workspaces: {
    list(runnerId?: string, signal?: AbortSignal): Promise<Workspace[]>;
    get(workspaceId: string, signal?: AbortSignal): Promise<Workspace>;
    create(
      data: Record<string, unknown>,
      signal?: AbortSignal,
    ): Promise<{
      workspace_id: string;
      task_id: string;
      status: string;
      plugin_ids: string[];
    }>;
    update(
      workspaceId: string,
      data: Record<string, unknown>,
      signal?: AbortSignal,
    ): Promise<Workspace>;
    delete(workspaceId: string, signal?: AbortSignal): Promise<void>;
    stop(workspaceId: string, signal?: AbortSignal): Promise<unknown>;
    resume(workspaceId: string, signal?: AbortSignal): Promise<unknown>;
  };
  conversations: {
    list(signal?: AbortSignal): Promise<HarnessConversation[]>;
  };
  harness: ReturnType<typeof createHarnessApi>;
  skills: {
    list(signal?: AbortSignal): Promise<Skill[]>;
    create(data: SkillCreateInput, signal?: AbortSignal): Promise<Skill>;
    update(
      skillId: string,
      data: SkillUpdateInput,
      signal?: AbortSignal,
    ): Promise<Skill>;
    delete(skillId: string, signal?: AbortSignal): Promise<void>;
  };
  realtime: RealtimeClient;
  files: Pick<
    RealtimeClient,
    "findFiles" | "listFiles" | "readFile" | "downloadFile" | "uploadFile"
  >;
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

/** Create an isolated API/realtime client instance without browser storage or framework state. */
export function createClient(options: CreateClientOptions): OpenCuriaClient {
  const apiOptions: ApiClientOptions = {
    apiBaseUrl: options.apiBaseUrl,
    credentials: "omit",
    getAccessToken: options.getAccessToken,
    getRefreshToken: options.getRefreshToken,
    setTokens: options.setTokens,
    getOrganizationId: options.getOrganizationId,
    onAuthenticationExpired: options.onAuthenticationExpired,
    fetch: options.fetch,
  };
  const api = new ApiClient(apiOptions) as ClientApi;
  Object.defineProperty(api, "baseUrl", {
    enumerable: true,
    get: () => api.getContext().baseUrl,
  });
  const realtimeOptions: RealtimeOptions = {
    wsBaseUrl: options.wsBaseUrl,
    getAccessToken: options.getAccessToken,
    getContextKey: () => {
      const context = api.getContext();
      return `${context.generation}:${context.baseUrl}:${context.organizationId ?? ""}`;
    },
    onContextChange: (callback) => api.onContextChange(callback),
    refreshAccessToken: () => api.refreshAccessToken(),
    socketFactory: options.socketFactory,
    timeoutMs: options.fileTransferTimeoutMs,
    createRequestId: options.createFileRequestId,
  };
  const realtime = new RealtimeClient(realtimeOptions);
  const harness = createHarnessApi(() => api);

  return {
    api,
    notifications: {
      capabilities: (signal) =>
        api.get<PushCapabilities>("/notifications/capabilities/", signal),
      registerDevice: (id, input, signal) =>
        api.put<MobileDevice>(
          `/notifications/devices/${segment(id)}/`,
          input,
          signal,
        ),
      unregisterDevice: (id, signal) =>
        api.delete<void>(`/notifications/devices/${segment(id)}/`, signal),
      registerActivity: (id, activityId, pushToken, signal) =>
        api.put<void>(
          `/notifications/devices/${segment(id)}/activity/`,
          { activity_id: activityId, push_token: pushToken },
          signal,
        ),
      activity: (id, signal) =>
        api.get<AgentActivitySnapshot>(
          `/notifications/devices/${segment(id)}/activity/`,
          signal,
        ),
    },
    auth: {
      async login(email, password) {
        const tokens = await api.post<TokenPair>("/auth/login/", {
          email,
          password,
        });
        await options.setTokens(tokens);
        api.notifyTokensChanged();
        return tokens;
      },
      me: (signal) => api.get<AuthUser>("/auth/me/", signal),
    },
    workspaces: {
      list: (runnerId, signal) =>
        api.get<Workspace[]>(
          `/workspaces/${runnerId ? `?runner_id=${encodeURIComponent(runnerId)}` : ""}`,
          signal,
        ),
      get: (workspaceId, signal) =>
        api.get<Workspace>(`/workspaces/${segment(workspaceId)}/`, signal),
      create: (data, signal) => {
        const { image_id: imageId, ...rest } = data;
        return api.post(
          "/workspaces/",
          {
            ...rest,
            ...(imageId === undefined ? {} : { image_artifact_id: imageId }),
          },
          signal,
        );
      },
      update: (workspaceId, data, signal) =>
        api.patch<Workspace>(
          `/workspaces/${segment(workspaceId)}/`,
          data,
          signal,
        ),
      delete: (workspaceId, signal) =>
        api.delete<void>(`/workspaces/${segment(workspaceId)}/`, signal),
      stop: (workspaceId, signal) =>
        api.post(
          `/workspaces/${segment(workspaceId)}/stop/`,
          undefined,
          signal,
        ),
      resume: (workspaceId, signal) =>
        api.post(
          `/workspaces/${segment(workspaceId)}/resume/`,
          undefined,
          signal,
        ),
    },
    conversations: {
      list: harness.listConversations,
    },
    harness,
    skills: {
      list: (signal) => api.get<Skill[]>("/skills/", signal),
      create: (data, signal) => api.post<Skill>("/skills/", data, signal),
      update: (skillId, data, signal) =>
        api.patch<Skill>(`/skills/${segment(skillId)}/`, data, signal),
      delete: (skillId, signal) =>
        api.delete<void>(`/skills/${segment(skillId)}/`, signal),
    },
    realtime,
    files: realtime,
  };
}
