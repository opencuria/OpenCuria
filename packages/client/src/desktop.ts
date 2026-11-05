import type {
  DesktopStatus,
  DesktopViewerIntent,
  DesktopViewerTransport,
} from "@opencuria/chat-core/desktopViewer";
import type { ApiClient } from "./api";

export type { DesktopStatus, DesktopViewerIntent };

function desktopPath(workspaceId: string, suffix = ""): string {
  return `/workspaces/${encodeURIComponent(workspaceId)}/desktop/${suffix}`;
}

/**
 * KasmVNC desktop of one workspace (same contract as the webapp). `start`,
 * `stop`, `renew` and `status` carry this client's viewer intent and
 * implement the `DesktopViewerTransport` of `@opencuria/chat-core/desktopViewer`.
 */
export function createDesktopApi(api: () => ApiClient) {
  return {
    /** Dispatch the desktop start for this viewer intent (202 acknowledgement). */
    start: (
      workspaceId: string,
      intent: DesktopViewerIntent,
      signal?: AbortSignal,
    ) =>
      api().post<{ task_id: string }>(desktopPath(workspaceId), intent, signal),
    /** Release this viewer intent; the desktop stops once nobody holds it. */
    stop: (
      workspaceId: string,
      intent: DesktopViewerIntent,
      signal?: AbortSignal,
    ) =>
      api().post<{ task_id: string }>(
        desktopPath(workspaceId, "stop/"),
        intent,
        signal,
      ),
    renew: (
      workspaceId: string,
      intent: DesktopViewerIntent,
      signal?: AbortSignal,
    ) =>
      api().post<DesktopStatus>(
        desktopPath(workspaceId, "renew/"),
        intent,
        signal,
      ),
    /** Desktop state; lease fields describe `viewerClientId`'s own intent. */
    status: (
      workspaceId: string,
      viewerClientId?: string,
      signal?: AbortSignal,
    ) => {
      const query = viewerClientId
        ? `?${new URLSearchParams({ viewer_client_id: viewerClientId })}`
        : "";
      return api().get<DesktopStatus>(
        desktopPath(workspaceId, `status/${query}`),
        signal,
      );
    },
    /** Abort the Computer Use agent driving the desktop. */
    takeControl: (workspaceId: string, signal?: AbortSignal) =>
      api().post<{ aborted_session_ids: string[] }>(
        desktopPath(workspaceId, "take-control/"),
        undefined,
        signal,
      ),
    readClipboard: (workspaceId: string, signal?: AbortSignal) =>
      api().post<{ text: string }>(
        desktopPath(workspaceId, "clipboard/read/"),
        undefined,
        signal,
      ),
    writeClipboard: (workspaceId: string, text: string, signal?: AbortSignal) =>
      api().post<{ text: string }>(
        desktopPath(workspaceId, "clipboard/write/"),
        { text },
        signal,
      ),
  };
}

export type DesktopApi = ReturnType<typeof createDesktopApi>;

/** Adapt the REST group to the shared viewer coordinator's port. */
export function desktopViewerTransport(
  desktop: () => Pick<DesktopApi, "start" | "stop" | "renew" | "status">,
): DesktopViewerTransport {
  return {
    start: (workspaceId, intent, signal) =>
      desktop().start(workspaceId, intent, signal),
    stop: (workspaceId, intent) => desktop().stop(workspaceId, intent),
    renew: (workspaceId, intent, signal) =>
      desktop().renew(workspaceId, intent, signal),
    status: (workspaceId, viewerClientId, signal) =>
      desktop().status(workspaceId, viewerClientId, signal),
  };
}
