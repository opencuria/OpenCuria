import { ApiClient, type ApiClientOptions, type FetchLike } from "./api";
import {
  RealtimeClient,
  type RealtimeOptions,
  type SocketFactory,
} from "./realtime";
import type {
  AgentConfig,
  AgentConfigInput,
  AuthUser,
  HarnessConversation,
  HarnessEngine,
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSteer,
  HarnessTimeline,
  ProviderModel,
  ProviderConfig,
  ProviderConfigInput,
  RecentModelUsage,
  Skill,
  SkillCreateInput,
  SkillUpdateInput,
  TokenPair,
  Workspace,
} from "./types";
import type {
  HarnessForkIn,
  HarnessMessageEditIn,
  HarnessMessageIn,
  HarnessPermissionResponse,
  HarnessSessionCreateIn,
  HarnessSessionMode,
  HarnessSessionPatchIn,
} from "@opencuria/chat-core/harness";

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
  harness: {
    listSessions(
      workspaceId: string,
      signal?: AbortSignal,
    ): Promise<HarnessSession[]>;
    createSession(
      workspaceId: string,
      input: HarnessSessionCreateIn,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    send(
      sessionId: string,
      input: HarnessMessageIn | string,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    timeline(sessionId: string, signal?: AbortSignal): Promise<HarnessTimeline>;
    getPart(
      sessionId: string,
      partId: string,
      signal?: AbortSignal,
    ): Promise<unknown>;
    abort(sessionId: string, signal?: AbortSignal): Promise<HarnessSession>;
    edit(
      sessionId: string,
      messageId: string,
      input: HarnessMessageEditIn,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    fork(
      sessionId: string,
      input?: HarnessForkIn,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    rename(
      sessionId: string,
      title: string,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    patchSession(
      sessionId: string,
      input: HarnessSessionPatchIn,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    deleteSession(sessionId: string, signal?: AbortSignal): Promise<void>;
    markRead(sessionId: string, signal?: AbortSignal): Promise<void>;
    markUnread(sessionId: string, signal?: AbortSignal): Promise<void>;
    steer(
      sessionId: string,
      prompt: string,
      signal?: AbortSignal,
    ): Promise<HarnessSteer>;
    withdraw(
      sessionId: string,
      steerId: string,
      signal?: AbortSignal,
    ): Promise<HarnessSteer>;
    dismissNotice(
      sessionId: string,
      messageId: string,
      signal?: AbortSignal,
    ): Promise<void>;
    resolvePermission(
      sessionId: string,
      request: HarnessPermissionRequest,
      response: HarnessPermissionResponse,
      signal?: AbortSignal,
    ): Promise<unknown>;
    resolveQuestion(
      sessionId: string,
      requestId: string,
      answers: string[],
      reject?: boolean,
      signal?: AbortSignal,
    ): Promise<unknown>;
    setMode(
      sessionId: string,
      mode: HarnessSessionMode,
      signal?: AbortSignal,
    ): Promise<HarnessSession>;
    listEngines(signal?: AbortSignal): Promise<HarnessEngine[]>;
    listEngineModels(
      engineId: string,
      signal?: AbortSignal,
    ): Promise<ProviderModel[]>;
    listAgentConfigs(signal?: AbortSignal): Promise<AgentConfig[]>;
    saveAgentConfigs(
      configs: AgentConfigInput[],
      signal?: AbortSignal,
    ): Promise<AgentConfig[]>;
    listRecentModels(signal?: AbortSignal): Promise<RecentModelUsage[]>;
    saveRecentModel(
      model: string,
      effort?: string,
      signal?: AbortSignal,
    ): Promise<RecentModelUsage>;
    getDefaultConfig(signal?: AbortSignal): Promise<ProviderConfig>;
    saveDefaultConfig(
      config: ProviderConfigInput,
      signal?: AbortSignal,
    ): Promise<ProviderConfig>;
    deleteDefaultConfig(signal?: AbortSignal): Promise<void>;
  };
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
    refreshCredentials: "omit",
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

  return {
    api,
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
      list: (signal) =>
        api.get<HarnessConversation[]>("/harness/conversations/", signal),
    },
    harness: {
      listSessions: (workspaceId, signal) =>
        api.get<HarnessSession[]>(
          `/workspaces/${segment(workspaceId)}/harness/sessions/`,
          signal,
        ),
      createSession: (workspaceId, input, signal) => {
        const body: HarnessSessionCreateIn = {
          prompt: input.prompt,
          agent_name: input.agent_name ?? "build",
          mode: input.mode ?? "build",
          model: input.model ?? "",
          reasoning_effort: input.reasoning_effort ?? "",
          skill_ids: input.skill_ids ?? [],
          harness_id: input.harness_id ?? "native",
        };
        return api.post<HarnessSession>(
          `/workspaces/${segment(workspaceId)}/harness/sessions/`,
          body,
          signal,
        );
      },
      send: (sessionId, input, signal) =>
        api.post<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}/message`,
          typeof input === "string" ? { prompt: input } : input,
          signal,
        ),
      timeline: (sessionId, signal) =>
        api.get<HarnessTimeline>(
          `/harness/sessions/${segment(sessionId)}/timeline`,
          signal,
        ),
      getPart: (sessionId, partId, signal) =>
        api.get(
          `/harness/sessions/${segment(sessionId)}/parts/${segment(partId)}`,
          signal,
        ),
      abort: (sessionId, signal) =>
        api.post<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}/abort`,
          undefined,
          signal,
        ),
      edit: (sessionId, messageId, input, signal) =>
        api.post<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}/messages/${segment(messageId)}/edit`,
          input,
          signal,
        ),
      fork: (sessionId, input = {}, signal) =>
        api.post<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}/fork`,
          input,
          signal,
        ),
      rename: (sessionId, title, signal) =>
        api.patch<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}`,
          { title },
          signal,
        ),
      patchSession: (sessionId, input, signal) =>
        api.patch<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}`,
          input,
          signal,
        ),
      deleteSession: (sessionId, signal) =>
        api.delete<void>(`/harness/sessions/${segment(sessionId)}`, signal),
      markRead: (sessionId, signal) =>
        api.post<void>(
          `/harness/sessions/${segment(sessionId)}/read`,
          undefined,
          signal,
        ),
      markUnread: (sessionId, signal) =>
        api.post<void>(
          `/harness/sessions/${segment(sessionId)}/unread`,
          undefined,
          signal,
        ),
      steer: (sessionId, prompt, signal) =>
        api.post<HarnessSteer>(
          `/harness/sessions/${segment(sessionId)}/steer`,
          { prompt },
          signal,
        ),
      withdraw: (sessionId, steerId, signal) =>
        api.delete<HarnessSteer>(
          `/harness/sessions/${segment(sessionId)}/steer/${segment(steerId)}`,
          signal,
        ),
      dismissNotice: (sessionId, messageId, signal) =>
        api.post<void>(
          `/harness/sessions/${segment(sessionId)}/messages/${segment(messageId)}/notice-dismiss`,
          undefined,
          signal,
        ),
      resolvePermission: (sessionId, request, response, signal) =>
        api.post(
          `/harness/sessions/${segment(sessionId)}/permissions/${segment(request.request_id)}`,
          { response },
          signal,
        ),
      resolveQuestion: (
        sessionId,
        requestId,
        answers,
        reject = false,
        signal,
      ) =>
        api.post(
          `/harness/sessions/${segment(sessionId)}/questions/${segment(requestId)}`,
          { answers, reject },
          signal,
        ),
      setMode: (sessionId, mode, signal) =>
        api.patch<HarnessSession>(
          `/harness/sessions/${segment(sessionId)}/mode`,
          { mode },
          signal,
        ),
      listEngines: (signal) =>
        api.get<HarnessEngine[]>("/harness/engines/", signal),
      listEngineModels: (engineId, signal) =>
        api.get<ProviderModel[]>(
          `/harness/engines/${encodeURIComponent(engineId)}/models/`,
          signal,
        ),
      listAgentConfigs: (signal) =>
        api.get<AgentConfig[]>("/agent-configs/", signal),
      saveAgentConfigs: (configs, signal) =>
        api.request<AgentConfig[]>(
          "PUT",
          "/agent-configs/",
          { configs },
          signal,
        ),
      listRecentModels: (signal) =>
        api.get<RecentModelUsage[]>("/recent-models/", signal),
      saveRecentModel: (model, effort = "", signal) =>
        api.request<RecentModelUsage>(
          "PUT",
          "/recent-models/",
          { model, effort },
          signal,
        ),
      getDefaultConfig: (signal) =>
        api.get<ProviderConfig>("/provider-config/", signal),
      saveDefaultConfig: (config, signal) =>
        api.request<ProviderConfig>("PUT", "/provider-config/", config, signal),
      deleteDefaultConfig: (signal) =>
        api.request<void>("DELETE", "/provider-config/", undefined, signal),
    },
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
