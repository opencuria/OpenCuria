import type { ImageDefinition, ImageDefinitionBuildSummary, RunnerImageBuild } from '@/types'

export const EMPTY_BUILD_SUMMARY: ImageDefinitionBuildSummary = {
  active: 0,
  building: 0,
  failed: 0,
  inactive: 0,
  removing: 0,
}

const DEFINITION_LOCKED_STATUSES = new Set([
  'pending_deletion',
  'deleting',
  'deleted',
  'delete_failed',
])

const DEFINITION_POLLING_STATUSES = new Set(['pending_deletion', 'deleting'])

const BUILD_POLLING_STATUSES = new Set([
  'pending',
  'building',
  'pending_deletion',
  'deleting',
])

const HIDDEN_DEFINITION_STATUSES = new Set(['deleted'])

const WORKSPACE_HIDDEN_DEFINITION_STATUSES = new Set([
  'deleted',
  'pending_deletion',
  'deleting',
  'delete_failed',
])

export type RunnerBuildActionId =
  | 'build'
  | 'rebuild'
  | 'activate'
  | 'deactivate'
  | 'remove'
  | 'retry'
  | 'retry_remove'
  | 'view_log'

export type RunnerBuildActionKind = 'primary' | 'secondary' | 'destructive' | 'ghost'

export type RunnerBuildConfirmKind = 'rebuild' | 'remove'

export interface RunnerBuildAction {
  id: RunnerBuildActionId
  label: string
  kind: RunnerBuildActionKind
  disabled?: boolean
  confirm?: RunnerBuildConfirmKind
}

export interface DefinitionActions {
  canDuplicate: boolean
  canEdit: boolean
  canDelete: boolean
  canRetryDelete: boolean
  canRestore: boolean
}

export function isDefinitionHidden(status: string | undefined): boolean {
  return HIDDEN_DEFINITION_STATUSES.has(status || 'active')
}

export function isDefinitionLocked(status: string | undefined): boolean {
  return DEFINITION_LOCKED_STATUSES.has(status || 'active')
}

export function definitionNeedsPolling(status: string | undefined): boolean {
  return DEFINITION_POLLING_STATUSES.has(status || '')
}

export function buildNeedsPolling(status: string | undefined): boolean {
  return BUILD_POLLING_STATUSES.has(status || '')
}

export function isDefinitionSelectableForWorkspace(definition: {
  is_active: boolean
  status: string
}): boolean {
  return definition.is_active && !WORKSPACE_HIDDEN_DEFINITION_STATUSES.has(definition.status)
}

export function formatRunnerBuildSummary(
  summary?: ImageDefinitionBuildSummary | null,
): string {
  const counts = summary ?? EMPTY_BUILD_SUMMARY
  const total =
    counts.active + counts.building + counts.failed + counts.inactive + counts.removing
  if (total === 0) return 'Not built on any runner'

  const parts = [
    `${counts.active} active`,
    `${counts.building} building`,
    `${counts.failed} failed`,
  ]
  if (counts.inactive) parts.push(`${counts.inactive} inactive`)
  if (counts.removing) parts.push(`${counts.removing} removing`)
  return parts.join(', ')
}

export function getDefinitionActions(definition: {
  is_standard: boolean
  status: string
}): DefinitionActions {
  if (definition.status === 'delete_failed') {
    return {
      canDuplicate: false,
      canEdit: false,
      canDelete: false,
      canRetryDelete: true,
      canRestore: true,
    }
  }

  const locked = isDefinitionLocked(definition.status)
  return {
    canDuplicate: !locked,
    canEdit: !definition.is_standard && !locked,
    canDelete: !definition.is_standard && !locked,
    canRetryDelete: false,
    canRestore: false,
  }
}

export function getRunnerBuildStatusLabel(
  status: string | null | undefined,
  runnerOnline: boolean,
): string {
  if (!status) return 'Not built'
  if (status === 'pending' && !runnerOnline) return 'Waiting for runner'
  if (status === 'pending' || status === 'building') return 'Building'
  if (status === 'active') return 'Active'
  if (status === 'deactivated') return 'Inactive'
  if (status === 'failed') return 'Failed'
  if (status === 'pending_deletion' || status === 'deleting') return 'Removing'
  if (status === 'delete_failed') return 'Remove failed'
  return status
}

export function getRunnerBuildActions(
  status: string | null | undefined,
  options: { definitionLocked?: boolean } = {},
): RunnerBuildAction[] {
  if (options.definitionLocked) return []
  if (!status || status === 'deleted') {
    return [{ id: 'build', label: 'Build', kind: 'primary' }]
  }

  switch (status) {
    case 'pending':
    case 'building':
      return [{ id: 'view_log', label: 'View log', kind: 'ghost' }]
    case 'active':
      return [
        { id: 'rebuild', label: 'Rebuild', kind: 'primary', confirm: 'rebuild' },
        { id: 'deactivate', label: 'Deactivate', kind: 'secondary' },
        { id: 'remove', label: 'Remove', kind: 'destructive', confirm: 'remove' },
      ]
    case 'deactivated':
      return [
        { id: 'activate', label: 'Activate', kind: 'primary' },
        { id: 'rebuild', label: 'Rebuild', kind: 'secondary', confirm: 'rebuild' },
        { id: 'remove', label: 'Remove', kind: 'destructive', confirm: 'remove' },
      ]
    case 'failed':
      return [
        { id: 'retry', label: 'Retry', kind: 'primary' },
        { id: 'view_log', label: 'View log', kind: 'ghost' },
        { id: 'remove', label: 'Remove', kind: 'destructive', confirm: 'remove' },
      ]
    case 'pending_deletion':
    case 'deleting':
      return []
    case 'delete_failed':
      return [{ id: 'retry_remove', label: 'Retry remove', kind: 'primary' }]
    default:
      return [{ id: 'build', label: 'Build', kind: 'primary' }]
  }
}

export function visibleImageDefinitions(
  definitions: ImageDefinition[],
): ImageDefinition[] {
  return definitions.filter((definition) => !isDefinitionHidden(definition.status))
}

export function isInFlightBuild(build: RunnerImageBuild | null | undefined): boolean {
  return buildNeedsPolling(build?.status)
}
