/**
 * Lifecycle mutations of a workspace with the same confirmations and feedback
 * as the webapp: native alerts before destructive steps, toasts afterwards.
 */
import { Alert } from "react-native";
import {
  recreateBase,
  recreateBlocker,
  workspaceRecreateOptions,
} from "@opencuria/chat-core/imageVersions";
import type { OpenCuriaClient, Workspace } from "@opencuria/client";
import { showToast } from "../ui/toast";

export function errorText(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

/**
 * Run a mutation; report failures as an error toast and resolve to success.
 * `onAccepted` runs before the confirmation toast, e.g. to close the sheet
 * so the toast appears on the screen beneath.
 */
async function run(
  action: () => Promise<unknown>,
  success: { title: string; message?: string },
  failure: string,
  onAccepted?: () => void,
): Promise<boolean> {
  try {
    await action();
    onAccepted?.();
    showToast({ ...success, tone: "info" });
    return true;
  } catch (cause) {
    showToast({
      title: failure,
      message: errorText(cause, "Please try again."),
      tone: "error",
    });
    return false;
  }
}

export function startWorkspace(
  client: OpenCuriaClient,
  workspace: Workspace,
): Promise<boolean> {
  return run(
    () => client.workspaces.resume(workspace.id),
    { title: "Starting workspace", message: `${workspace.name} is starting…` },
    "Start failed",
  );
}

export function stopWorkspace(
  client: OpenCuriaClient,
  workspace: Workspace,
): Promise<boolean> {
  return run(
    () => client.workspaces.stop(workspace.id),
    { title: "Stopping workspace", message: `${workspace.name} is stopping…` },
    "Stop failed",
  );
}

/** Ask, then queue the removal; it continues if the runner is offline. */
export function confirmRemoveWorkspace(
  client: OpenCuriaClient,
  workspace: Workspace,
  onAccepted?: () => void,
): Promise<boolean> {
  return new Promise((resolve) => {
    Alert.alert(
      workspace.status === "delete_failed"
        ? "Retry removal?"
        : "Remove workspace?",
      `“${workspace.name}” and everything stored only inside it will be removed. This cannot be undone.`,
      [
        { text: "Cancel", style: "cancel", onPress: () => resolve(false) },
        {
          text: "Remove",
          style: "destructive",
          onPress: () => {
            void run(
              () => client.workspaces.delete(workspace.id),
              workspace.runner_online
                ? {
                    title: "Removing workspace",
                    message: `${workspace.name} is being removed…`,
                  }
                : {
                    title: "Removal queued",
                    message: "Removal continues when the runner reconnects.",
                  },
              "Removal failed",
              onAccepted,
            ).then(resolve);
          },
        },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}

/**
 * Reset to the workspace's own image version or update it to the latest one.
 * One native alert picks the target and confirms the data loss.
 */
export function confirmRecreateWorkspace(
  client: OpenCuriaClient,
  workspace: Workspace,
): Promise<boolean> {
  const options = workspaceRecreateOptions(workspace);
  const blocker = options.length
    ? null
    : recreateBlocker(recreateBase(workspace));
  return new Promise((resolve) => {
    if (blocker) {
      Alert.alert("Cannot reset workspace", blocker, [
        { text: "OK", onPress: () => resolve(false) },
      ]);
      return;
    }
    const repos = workspace.repos?.length
      ? `\n\nCloned again: ${workspace.repos.join(", ")}`
      : "";
    Alert.alert(
      options.some((option) => option.kind === "update")
        ? "Reset or update workspace?"
        : "Reset workspace?",
      `Recreates “${workspace.name}” from ${recreateBase(workspace)?.name ?? "its image"}. Files, installed packages, uncommitted changes and running processes are permanently deleted. Chats, settings, credentials and schedules are kept.${repos}`,
      [
        { text: "Cancel", style: "cancel", onPress: () => resolve(false) },
        ...options.map((option) => ({
          text:
            option.kind === "update"
              ? `Update to v${option.version ?? "?"}`
              : `Reset to v${option.version ?? "?"}`,
          style: "destructive" as const,
          onPress: () => {
            void run(
              () => client.workspaces.recreate(workspace.id, option.id),
              {
                title:
                  option.kind === "update"
                    ? "Updating workspace"
                    : "Resetting workspace",
                message: `${workspace.name} is being recreated from its image…`,
              },
              "Reset failed",
            ).then(resolve);
          },
        })),
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}
