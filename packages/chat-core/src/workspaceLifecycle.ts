/**
 * Workspace lifecycle rules shared by the web and native clients: which
 * actions a workspace admits and how its state is labelled. The server stays
 * authoritative for admission; these rules only decide what clients offer.
 */

/** Minimal workspace fields the lifecycle rules read. */
export interface WorkspaceLifecycleInput {
  status: string;
  runner_online: boolean;
  active_operation?: string | null;
  intervention_required?: boolean;
  credentials_present?: boolean;
  has_active_session?: boolean;
  base_image?: unknown | null;
  pending_base_image?: unknown | null;
}

export interface WorkspaceActionAvailability {
  start: boolean;
  stop: boolean;
  edit: boolean;
  capture: boolean;
  reset: boolean;
  remove: boolean;
}

/** Visual weight of a workspace's state, independent of any color system. */
export type WorkspaceStatusTone =
  | "running"
  | "busy"
  | "stopped"
  | "attention"
  | "error"
  | "offline"
  | "removed";

const LIFECYCLE_BUSY_STATUSES = new Set([
  "creating",
  "pending_deletion",
  "deleting",
  "deleted",
]);

const OPERATION_LABELS: Record<string, string> = {
  creating: "Creating…",
  starting: "Starting…",
  stopping: "Stopping…",
  restarting: "Restarting…",
  removing: "Removing…",
  resetting: "Resetting…",
  updating: "Updating…",
  synchronizing_credentials: "Syncing credentials…",
};

/** Include persisted transitions even before their operation event arrives. */
export function isWorkspaceLifecycleBusy(
  workspace: WorkspaceLifecycleInput,
): boolean {
  return Boolean(
    workspace.active_operation ||
    workspace.intervention_required ||
    LIFECYCLE_BUSY_STATUSES.has(workspace.status),
  );
}

/** Shared list/detail controls; the server remains authoritative for admission. */
export function getWorkspaceActionAvailability(
  workspace: WorkspaceLifecycleInput,
  transitioning = false,
): WorkspaceActionAvailability {
  const busy = transitioning || isWorkspaceLifecycleBusy(workspace);
  const online = workspace.runner_online;
  const running = workspace.status === "running";
  const stopped = workspace.status === "stopped";
  const editable = running || stopped || workspace.status === "failed";
  return {
    start: !busy && online && stopped,
    stop: !busy && online && running,
    edit: !busy && online && editable,
    capture:
      !busy &&
      online &&
      (running || (stopped && !workspace.credentials_present)),
    reset:
      !busy &&
      online &&
      editable &&
      !!(workspace.base_image || workspace.pending_base_image),
    // Removal can be durably queued while the runner is offline.
    remove: !busy && (editable || workspace.status === "delete_failed"),
  };
}

/**
 * Label of a persisted transition ("Starting…", "Removal queued"), or null
 * when the workspace is at rest. Clients may add optimistic labels for
 * requests the server has not reflected yet.
 */
export function workspaceOperationLabel(
  workspace: WorkspaceLifecycleInput,
): string | null {
  if (workspace.intervention_required) return "Checking state";
  if (workspace.active_operation === "capturing_image") return "Capturing";
  if (workspace.status === "pending_deletion") return "Removal queued";
  const operation = workspace.active_operation
    ? OPERATION_LABELS[workspace.active_operation]
    : undefined;
  if (operation) return operation;
  if (workspace.status === "creating") return "Creating…";
  if (workspace.status === "deleting") return "Removing…";
  return null;
}

function isRunnerOffline(workspace: WorkspaceLifecycleInput): boolean {
  return !workspace.runner_online && workspace.status !== "deleted";
}

/** One short status label, as on the webapp's workspace cards. */
export function workspaceStatusLabel(
  workspace: WorkspaceLifecycleInput,
  operationLabel: string | null = workspaceOperationLabel(workspace),
): string {
  if (isRunnerOffline(workspace)) return "Runner offline";
  if (workspace.intervention_required) return "Checking state";
  if (operationLabel) return operationLabel;
  if (workspace.status === "running" && workspace.has_active_session)
    return "Busy";
  const label = workspace.status.replace(/_/g, " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** Tone for the status label and icon of a workspace. */
export function workspaceStatusTone(
  workspace: WorkspaceLifecycleInput,
  operationLabel: string | null = workspaceOperationLabel(workspace),
): WorkspaceStatusTone {
  if (workspace.status === "deleted") return "removed";
  if (isRunnerOffline(workspace)) return "offline";
  if (workspace.intervention_required) return "attention";
  if (workspace.status === "failed" || workspace.status === "delete_failed")
    return "error";
  if (operationLabel) return "busy";
  if (workspace.status === "running")
    return workspace.has_active_session ? "busy" : "running";
  return "stopped";
}
