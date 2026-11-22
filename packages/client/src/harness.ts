import type {
  HarnessForkIn,
  HarnessMessageEditIn,
  HarnessMessageIn,
  HarnessPart,
  HarnessPermissionResponse,
  HarnessScheduledMessage,
  HarnessScheduledMessageIn,
  HarnessSessionCreateIn,
  HarnessSessionMode,
  HarnessSessionPatchIn,
} from "@opencuria/chat-core/harness";
import type { ApiClient } from "./api";
import type {
  AgentConfig,
  AgentConfigInput,
  HarnessConversation,
  HarnessEngine,
  HarnessPermissionRequest,
  HarnessSession,
  HarnessSteer,
  HarnessTimeline,
  HarnessTimelineRange,
  HarnessTodo,
  ProviderConfig,
  ProviderConfigInput,
  ProviderModel,
  RecentModelUsage,
} from "./types";

/** Query string bounding a timeline to a range of messages. */
function timelineQuery(range?: HarnessTimelineRange): string {
  const query = new URLSearchParams();
  if (range?.limit !== undefined) query.set("limit", String(range.limit));
  if (range?.before !== undefined) query.set("before", String(range.before));
  if (range?.since !== undefined) query.set("since", String(range.since));
  const text = query.toString();
  return text ? `?${text}` : "";
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

/** Share chat routes while resolving the caller's current transport and context. */
export function createHarnessApi(getApi: () => ApiClient) {
  const request = <T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> =>
    getApi().request<T>(method, path, body, signal);

  return {
    listConversations: (signal?: AbortSignal) =>
      request<HarnessConversation[]>(
        "GET",
        "/harness/conversations/",
        undefined,
        signal,
      ),
    listSessions: (workspaceId: string, signal?: AbortSignal) =>
      request<HarnessSession[]>(
        "GET",
        `/workspaces/${segment(workspaceId)}/harness/sessions/`,
        undefined,
        signal,
      ),
    createSession: (
      workspaceId: string,
      input: HarnessSessionCreateIn,
      signal?: AbortSignal,
    ) =>
      request<HarnessSession>(
        "POST",
        `/workspaces/${segment(workspaceId)}/harness/sessions/`,
        input,
        signal,
      ),
    send: (sessionId: string, input: HarnessMessageIn, signal?: AbortSignal) =>
      request<HarnessSession>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/message`,
        input,
        signal,
      ),
    timeline: (
      sessionId: string,
      signal?: AbortSignal,
      range?: HarnessTimelineRange,
    ) =>
      request<HarnessTimeline>(
        "GET",
        `/harness/sessions/${segment(sessionId)}/timeline${timelineQuery(range)}`,
        undefined,
        signal,
      ),
    getPart: (sessionId: string, partId: string, signal?: AbortSignal) =>
      request<HarnessPart>(
        "GET",
        `/harness/sessions/${segment(sessionId)}/parts/${segment(partId)}`,
        undefined,
        signal,
      ),
    listTodos: (sessionId: string, signal?: AbortSignal) =>
      request<HarnessTodo[]>(
        "GET",
        `/harness/sessions/${segment(sessionId)}/todos`,
        undefined,
        signal,
      ),
    abort: (sessionId: string, signal?: AbortSignal) =>
      request<HarnessSession>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/abort`,
        undefined,
        signal,
      ),
    edit: (
      sessionId: string,
      messageId: string,
      input: HarnessMessageEditIn,
      signal?: AbortSignal,
    ) =>
      request<HarnessSession>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/messages/${segment(messageId)}/edit`,
        input,
        signal,
      ),
    fork: (
      sessionId: string,
      input: HarnessForkIn = {},
      signal?: AbortSignal,
    ) =>
      request<HarnessSession>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/fork`,
        input,
        signal,
      ),
    rename: (sessionId: string, title: string, signal?: AbortSignal) =>
      request<HarnessSession>(
        "PATCH",
        `/harness/sessions/${segment(sessionId)}`,
        { title },
        signal,
      ),
    patchSession: (
      sessionId: string,
      input: HarnessSessionPatchIn,
      signal?: AbortSignal,
    ) =>
      request<HarnessSession>(
        "PATCH",
        `/harness/sessions/${segment(sessionId)}`,
        input,
        signal,
      ),
    deleteSession: (sessionId: string, signal?: AbortSignal) =>
      request<void>(
        "DELETE",
        `/harness/sessions/${segment(sessionId)}`,
        undefined,
        signal,
      ),
    markRead: (sessionId: string, signal?: AbortSignal) =>
      request<void>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/read`,
        undefined,
        signal,
      ),
    markUnread: (sessionId: string, signal?: AbortSignal) =>
      request<void>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/unread`,
        undefined,
        signal,
      ),
    steer: (sessionId: string, prompt: string, signal?: AbortSignal) =>
      request<HarnessSteer>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/steer`,
        { prompt },
        signal,
      ),
    withdraw: (sessionId: string, steerId: string, signal?: AbortSignal) =>
      request<HarnessSteer>(
        "DELETE",
        `/harness/sessions/${segment(sessionId)}/steer/${segment(steerId)}`,
        undefined,
        signal,
      ),
    scheduleMessage: (
      sessionId: string,
      input: HarnessScheduledMessageIn,
      signal?: AbortSignal,
    ) =>
      request<HarnessScheduledMessage>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/scheduled-messages`,
        input,
        signal,
      ),
    rescheduleMessage: (
      sessionId: string,
      messageId: string,
      sendAt: string,
      signal?: AbortSignal,
    ) =>
      request<HarnessScheduledMessage>(
        "PATCH",
        `/harness/sessions/${segment(sessionId)}/scheduled-messages/${segment(messageId)}`,
        { send_at: sendAt },
        signal,
      ),
    withdrawScheduledMessage: (
      sessionId: string,
      messageId: string,
      signal?: AbortSignal,
    ) =>
      request<HarnessScheduledMessage>(
        "DELETE",
        `/harness/sessions/${segment(sessionId)}/scheduled-messages/${segment(messageId)}`,
        undefined,
        signal,
      ),
    sendScheduledMessage: (
      sessionId: string,
      messageId: string,
      signal?: AbortSignal,
    ) =>
      request<HarnessSession>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/scheduled-messages/${segment(messageId)}/send`,
        undefined,
        signal,
      ),
    dismissNotice: (
      sessionId: string,
      messageId: string,
      signal?: AbortSignal,
    ) =>
      request<void>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/messages/${segment(messageId)}/notice-dismiss`,
        undefined,
        signal,
      ),
    resolvePermission: (
      sessionId: string,
      permission: HarnessPermissionRequest,
      response: HarnessPermissionResponse,
      signal?: AbortSignal,
    ) =>
      request<{ request_id: string; decision: string; remember: string }>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/permissions/${segment(permission.request_id)}`,
        { response },
        signal,
      ),
    resolveQuestion: (
      sessionId: string,
      requestId: string,
      answers: string[],
      reject = false,
      signal?: AbortSignal,
    ) =>
      request<{ request_id: string; status: string }>(
        "POST",
        `/harness/sessions/${segment(sessionId)}/questions/${segment(requestId)}`,
        { answers, reject },
        signal,
      ),
    setMode: (
      sessionId: string,
      mode: HarnessSessionMode,
      signal?: AbortSignal,
    ) =>
      request<HarnessSession>(
        "PATCH",
        `/harness/sessions/${segment(sessionId)}/mode`,
        { mode },
        signal,
      ),
    listEngines: (signal?: AbortSignal) =>
      request<HarnessEngine[]>("GET", "/harness/engines/", undefined, signal),
    listEngineModels: (engineId: string, signal?: AbortSignal) =>
      request<ProviderModel[]>(
        "GET",
        `/harness/engines/${segment(engineId)}/models/`,
        undefined,
        signal,
      ),
    listAgentConfigs: (signal?: AbortSignal) =>
      request<AgentConfig[]>("GET", "/agent-configs/", undefined, signal),
    saveAgentConfigs: (configs: AgentConfigInput[], signal?: AbortSignal) =>
      request<AgentConfig[]>("PUT", "/agent-configs/", { configs }, signal),
    listRecentModels: (signal?: AbortSignal) =>
      request<RecentModelUsage[]>("GET", "/recent-models/", undefined, signal),
    saveRecentModel: (
      model: string,
      effort = "",
      signal?: AbortSignal,
      selection: Pick<RecentModelUsage, "harness_id" | "mode"> = {},
    ) =>
      request<RecentModelUsage>(
        "PUT",
        "/recent-models/",
        { model, effort, ...selection },
        signal,
      ),
    getDefaultConfig: (signal?: AbortSignal) =>
      request<ProviderConfig>("GET", "/provider-config/", undefined, signal),
    saveDefaultConfig: (config: ProviderConfigInput, signal?: AbortSignal) =>
      request<ProviderConfig>("PUT", "/provider-config/", config, signal),
  };
}
