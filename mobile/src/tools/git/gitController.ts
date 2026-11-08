import type { OpenCuriaClient } from "@opencuria/client";
import { GitController, type GitNotice } from "@opencuria/chat-core/gitController";
import { NativeGitSelectionStorage } from "../../platform/gitSelection";
import { showToast } from "../../ui/toast";

/** Serialize preference reads/writes across controller lifetimes. */
const selectedRepos = new NativeGitSelectionStorage();

/**
 * Shared Git controller of one workspace for the native Git panel. The
 * selected repository is remembered per server, organization and workspace.
 */
export function createGitController(
  client: OpenCuriaClient,
  workspaceId: string,
  notify: (notice: GitNotice) => void = showToast,
): GitController {
  const { baseUrl, organizationId } = client.api.getContext();
  const scope = JSON.stringify([baseUrl, organizationId, workspaceId]);
  return new GitController(client.git, workspaceId, {
    notify,
    selection: {
      load: () => selectedRepos.load(scope),
      save: (path) => selectedRepos.save(scope, path),
    },
  });
}
