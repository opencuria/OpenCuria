import { ApiClient, type ApiClientOptions, type FetchLike } from "./api";
import { createHarnessApi } from "./harness";
import {
  createAgentSettingsApi,
  createApiKeysApi,
  createCredentialServicesApi,
  createCredentialsApi,
  createDeletionsApi,
  createImagesApi,
  createOrganizationsApi,
  createPluginsApi,
  createProviderAccountsApi,
  createRunnersApi,
  createScheduledTasksApi,
  createSkillsApi,
} from "./settings";
import { createGitApi, createTerminalApi } from "./workspaceTools";
import { createAppsApi } from "./apps";
import { createDesktopApi } from "./desktop";
import { buildDesktopViewerUrl } from "@opencuria/chat-core/desktopSurface";
import type { DesktopViewerIntent } from "@opencuria/chat-core/desktopViewer";
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
  ImageCaptureInput,
  ImageCaptureResult,
  TokenPair,
  Workspace,
  WorkspaceCreateInput,
  WorkspaceCreateResult,
  WorkspaceRecreateResult,
  WorkspaceTask,
  WorkspaceUpdateInput,
  WorkspaceUpdateResult,
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
  requestTimeoutMs?: number;
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
      data: WorkspaceCreateInput,
      signal?: AbortSignal,
    ): Promise<WorkspaceCreateResult>;
    update(
      workspaceId: string,
      data: WorkspaceUpdateInput,
      signal?: AbortSignal,
    ): Promise<WorkspaceUpdateResult>;
    /** Removal is queued durably, also while the runner is offline. */
    delete(workspaceId: string, signal?: AbortSignal): Promise<WorkspaceTask>;
    stop(workspaceId: string, signal?: AbortSignal): Promise<WorkspaceTask>;
    resume(workspaceId: string, signal?: AbortSignal): Promise<WorkspaceTask>;
    /** Reset (own version) or update (latest version); workspace data is lost. */
    recreate(
      workspaceId: string,
      imageId: string,
      signal?: AbortSignal,
    ): Promise<WorkspaceRecreateResult>;
    /** Capture the workspace as a new image or the next version of one. */
    captureImage(
      workspaceId: string,
      data: ImageCaptureInput,
      signal?: AbortSignal,
    ): Promise<ImageCaptureResult>;
  };
  /** Registration, QEMU limits, metrics, storage inventory and operations. */
  runners: ReturnType<typeof createRunnersApi>;
  /** The plugin catalog; edits and activation need an organization admin. */
  plugins: ReturnType<typeof createPluginsApi>;
  /** Credentials, their services and OAuth grants. */
  credentials: ReturnType<typeof createCredentialsApi>;
  /** Organization-owned credential services (admins only). */
  credentialServices: ReturnType<typeof createCredentialServicesApi>;
  /** Organization profile, creation and workspace policy. */
  organizations: ReturnType<typeof createOrganizationsApi>;
  /** Model provider accounts, selection and ChatGPT device login. */
  providerAccounts: ReturnType<typeof createProviderAccountsApi>;
  /** Subagent depth, Computer Use limits and voice models. */
  agentSettings: ReturnType<typeof createAgentSettingsApi>;
  /** The signed-in user's API keys. */
  apiKeys: ReturnType<typeof createApiKeysApi>;
  /** Deferred or forced removal of images, builds and definitions. */
  deletions: ReturnType<typeof createDeletionsApi>;
  /** Captured images, image definitions and their builds on runners. */
  images: ReturnType<typeof createImagesApi>;
  conversations: {
    list(signal?: AbortSignal): Promise<HarnessConversation[]>;
  };
  /** The signed-in user's scheduled tasks in the active organization. */
  scheduledTasks: ReturnType<typeof createScheduledTasksApi>;
  harness: ReturnType<typeof createHarnessApi>;
  /** Expo apps agents published to the signed-in user. */
  apps: ReturnType<typeof createAppsApi>;
  /** Workspace git integration (repos, history, typed mutations, file sides). */
  git: ReturnType<typeof createGitApi>;
  /** KasmVNC desktop: viewer lease, Computer Use takeover and clipboard. */
  desktop: ReturnType<typeof createDesktopApi> & {
    /** KasmVNC client URL for a ready desktop route, with a fresh ticket. */
    viewerUrl(
      workspaceId: string,
      proxyUrl: string,
      intent?: DesktopViewerIntent,
      signal?: AbortSignal,
    ): Promise<string>;
  };
  /** Workspace PTY: REST start plus Socket.IO input/resize/close. */
  terminal: ReturnType<typeof createTerminalApi>;
  /** Personal and organization skills. */
  skills: ReturnType<typeof createSkillsApi>;
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
    requestTimeoutMs: options.requestTimeoutMs,
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
        return api.post<WorkspaceCreateResult>(
          "/workspaces/",
          { ...rest, image_artifact_id: imageId },
          signal,
        );
      },
      update: (workspaceId, data, signal) =>
        api.patch<WorkspaceUpdateResult>(
          `/workspaces/${segment(workspaceId)}/`,
          data,
          signal,
        ),
      delete: (workspaceId, signal) =>
        api.delete<WorkspaceTask>(
          `/workspaces/${segment(workspaceId)}/`,
          signal,
        ),
      stop: (workspaceId, signal) =>
        api.post<WorkspaceTask>(
          `/workspaces/${segment(workspaceId)}/stop/`,
          undefined,
          signal,
        ),
      resume: (workspaceId, signal) =>
        api.post<WorkspaceTask>(
          `/workspaces/${segment(workspaceId)}/resume/`,
          undefined,
          signal,
        ),
      recreate: (workspaceId, imageId, signal) =>
        api.post<WorkspaceRecreateResult>(
          `/workspaces/${segment(workspaceId)}/recreate/`,
          { image_id: imageId },
          signal,
        ),
      captureImage: (workspaceId, data, signal) =>
        api.post<ImageCaptureResult>(
          `/workspaces/${segment(workspaceId)}/image-artifacts/`,
          data,
          signal,
        ),
    },
    runners: createRunnersApi(() => api),
    plugins: createPluginsApi(() => api),
    credentials: createCredentialsApi(() => api),
    credentialServices: createCredentialServicesApi(() => api),
    organizations: createOrganizationsApi(() => api),
    providerAccounts: createProviderAccountsApi(() => api),
    agentSettings: createAgentSettingsApi(() => api),
    apiKeys: createApiKeysApi(() => api),
    deletions: createDeletionsApi(() => api),
    images: createImagesApi(() => api),
    conversations: {
      list: harness.listConversations,
    },
    scheduledTasks: createScheduledTasksApi(() => api),
    harness,
    apps: createAppsApi(() => api, options.wsBaseUrl),
    git: createGitApi(() => api),
    desktop: (() => {
      const desktop = createDesktopApi(() => api);
      return {
        ...desktop,
        viewerUrl: async (workspaceId, proxyUrl, intent, signal) => {
          const { ticket } = await desktop.ticket(workspaceId, signal);
          return buildDesktopViewerUrl(
            options.wsBaseUrl,
            proxyUrl,
            ticket,
            intent,
          );
        },
      };
    })(),
    terminal: createTerminalApi(() => api, realtime),
    skills: createSkillsApi(() => api),
    realtime,
    files: realtime,
  };
}
