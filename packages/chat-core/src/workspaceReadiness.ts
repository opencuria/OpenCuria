/** Minimal inputs used by chat workspace readiness selectors. */
export interface WorkspaceReadinessInput {
  status: string;
  runner_online: boolean;
  active_operation?: string | null;
  intervention_required?: boolean;
}

/** Whether a workspace can accept chat prompts given runtime and transition state. */
export function isWorkspaceReadyForChat(
  workspace: WorkspaceReadinessInput,
  transitioning = false,
): boolean {
  return (
    workspace.status === "running" &&
    workspace.runner_online &&
    !workspace.active_operation &&
    !workspace.intervention_required &&
    !transitioning
  );
}
