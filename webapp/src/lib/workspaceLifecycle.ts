import { WorkspaceStatus, type Workspace } from '@/types'

type WorkspaceLifecycle = Pick<
  Workspace,
  'status' | 'runner_online' | 'active_operation' | 'intervention_required'
> &
  Partial<Pick<Workspace, 'credentials_present' | 'base_image' | 'pending_base_image'>>

/** Include persisted transitions even before their operation event arrives. */
export function isWorkspaceLifecycleBusy(workspace: WorkspaceLifecycle): boolean {
  return Boolean(
    workspace.active_operation ||
    workspace.intervention_required ||
    [
      WorkspaceStatus.CREATING,
      WorkspaceStatus.PENDING_DELETION,
      WorkspaceStatus.DELETING,
      WorkspaceStatus.DELETED,
    ].includes(workspace.status),
  )
}

/** Shared list/detail controls; the server remains authoritative for admission. */
export function getWorkspaceActionAvailability(
  workspace: WorkspaceLifecycle,
  transitioning = false,
) {
  const busy = transitioning || isWorkspaceLifecycleBusy(workspace)
  const online = workspace.runner_online
  const running = workspace.status === WorkspaceStatus.RUNNING
  const stopped = workspace.status === WorkspaceStatus.STOPPED
  const editable = running || stopped || workspace.status === WorkspaceStatus.FAILED
  return {
    start: !busy && online && stopped,
    stop: !busy && online && running,
    edit: !busy && online && editable,
    capture: !busy && online && (running || (stopped && !workspace.credentials_present)),
    reset: !busy && online && editable && !!(workspace.base_image || workspace.pending_base_image),
    // Removal can be durably queued while the runner is offline.
    remove: !busy && (editable || workspace.status === WorkspaceStatus.DELETE_FAILED),
  }
}
