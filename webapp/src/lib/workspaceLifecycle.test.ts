import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { WorkspaceOperation, WorkspaceStatus, type Workspace } from '@/types'
import { useWorkspaceStore } from '@/stores/workspaces'
import { getWorkspaceActionAvailability } from './workspaceLifecycle'

const states: [WorkspaceStatus, string[]][] = [
  [WorkspaceStatus.CREATING, []],
  [WorkspaceStatus.RUNNING, ['stop', 'edit', 'capture', 'reset', 'remove']],
  [WorkspaceStatus.STOPPED, ['start', 'edit', 'capture', 'reset', 'remove']],
  [WorkspaceStatus.FAILED, ['edit', 'reset', 'remove']],
  [WorkspaceStatus.PENDING_DELETION, []],
  [WorkspaceStatus.DELETING, []],
  [WorkspaceStatus.DELETED, []],
  [WorkspaceStatus.DELETE_FAILED, ['remove']],
]
let index = 0
const cases = states.flatMap(([status, onlineActions]) =>
  [true, false].flatMap((runner_online) =>
    [false, true].map((intervention_required) => ({
      id: `F${String(++index).padStart(3, '0')}`,
      status,
      runner_online,
      intervention_required,
      enabled: intervention_required
        ? []
        : runner_online
          ? onlineActions
          : onlineActions.filter((action) => action === 'remove'),
    })),
  ),
)

beforeEach(() => setActivePinia(createPinia()))

describe('canonical lifecycle state/action matrix', () => {
  it.each(cases)('$id $status online=$runner_online recovery=$intervention_required', (row) => {
    const workspace = {
      ...row,
      id: 'workspace-matrix',
      active_operation: null,
      credentials_present: false,
      base_image: { id: 'image-1' },
    } as unknown as Workspace
    const store = useWorkspaceStore()
    store.workspaces = [workspace]
    const actions = getWorkspaceActionAvailability(
      workspace,
      store.isWorkspaceTransitioning(workspace.id),
    )
    expect(
      Object.entries(actions)
        .filter(([, enabled]) => enabled)
        .map(([action]) => action),
    ).toEqual(row.enabled)
    expect(store.canUseWorkspace(workspace.id)).toBe(
      row.status === WorkspaceStatus.RUNNING && row.runner_online && !row.intervention_required,
    )
    if (row.intervention_required)
      expect(store.getWorkspaceTransitionLabel(workspace.id)).toBe('Checking state')
  })

  it.each([
    ['F033', WorkspaceOperation.CREATING, 'Creating…'],
    ['F034', WorkspaceOperation.STARTING, 'Starting…'],
    ['F035', WorkspaceOperation.STOPPING, 'Stopping…'],
    ['F036', WorkspaceOperation.RESTARTING, 'Restarting…'],
    ['F037', WorkspaceOperation.REMOVING, 'Removing…'],
    ['F038', WorkspaceOperation.CAPTURING_IMAGE, 'Capturing'],
    ['F039', WorkspaceOperation.RESETTING, 'Resetting…'],
    ['F040', WorkspaceOperation.UPDATING, 'Updating…'],
  ] as const)('%s active %s blocks every mutation', (_id, active_operation, label) => {
    const workspace = {
      id: 'workspace-operation',
      status: WorkspaceStatus.RUNNING,
      runner_online: true,
      active_operation,
      base_image: { id: 'image-1' },
    } as Workspace
    const store = useWorkspaceStore()
    store.workspaces = [workspace]
    expect(store.getWorkspaceTransitionLabel(workspace.id)).toBe(label)
    expect(Object.values(getWorkspaceActionAvailability(workspace))).not.toContain(true)
    expect(store.canUseWorkspace(workspace.id)).toBe(false)
  })
})
