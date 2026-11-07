import type { ApiClient } from "./api";
import type {
  AppFileRequest,
  AppManifest,
  PublishedApp,
  PublishedAppDetail,
} from "./types";

function appPath(appId: string, suffix = ""): string {
  return `/apps/${encodeURIComponent(appId)}/${suffix}`;
}

function versionPath(appId: string, versionId: string, suffix: string): string {
  return appPath(appId, `versions/${encodeURIComponent(versionId)}/${suffix}`);
}

/**
 * Expo apps agents published to the signed-in user. Agents publish through
 * the `app_publish` MCP tool; clients list apps, read a version's install
 * manifest and download its content-addressed files.
 */
export function createAppsApi(api: () => ApiClient) {
  return {
    list: (signal?: AbortSignal) => api().get<PublishedApp[]>("/apps/", signal),
    get: (appId: string, signal?: AbortSignal) =>
      api().get<PublishedAppDetail>(appPath(appId), signal),
    /** Remove the app from the server; installed copies keep working. */
    delete: (appId: string, signal?: AbortSignal) =>
      api().delete<void>(appPath(appId), signal),
    manifest: (
      appId: string,
      versionId: string,
      platform: AppManifest["platform"],
      signal?: AbortSignal,
    ) =>
      api().get<AppManifest>(
        versionPath(
          appId,
          versionId,
          `manifest/?${new URLSearchParams({ platform })}`,
        ),
        signal,
      ),
    /**
     * Authenticated request for a version file (bundle, asset or icon) with
     * the current session, for native downloaders. Fetch the manifest first so
     * an expired access token is refreshed.
     */
    fileRequest: (
      appId: string,
      versionId: string,
      sha256: string,
    ): AppFileRequest => {
      const context = api().getContext();
      const headers: Record<string, string> = {};
      if (context.accessToken)
        headers.Authorization = `Bearer ${context.accessToken}`;
      if (context.organizationId)
        headers["X-Organization-Id"] = context.organizationId;
      return {
        url: `${context.baseUrl}${versionPath(appId, versionId, `files/${encodeURIComponent(sha256)}/`)}`,
        headers,
      };
    },
  };
}
