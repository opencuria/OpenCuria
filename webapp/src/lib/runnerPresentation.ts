import type { StorageGeneration } from '@/types/runnerStorage'

export function currentRunnerDefault(image: StorageGeneration): boolean {
  return (
    image.is_current &&
    image.status !== 'deleted' &&
    !['deleted', 'retired', 'deactivated'].includes(image.assignment_status ?? '')
  )
}

export function generationLabel(image: StorageGeneration): string {
  if (image.status === 'deleted') return 'Deleted'
  if (currentRunnerDefault(image)) return 'Current default'
  if (image.is_pending) return 'Pending attempt'
  return image.origin_type === 'workspace_capture' ? 'Capture' : 'History'
}

export function runnerStateClass(state: string | null | undefined): string {
  if (
    [
      'failed',
      'error',
      'missing',
      'intervention',
      'intervention_required',
      'delete_failed',
    ].includes(state ?? '')
  )
    return 'border-destructive/20 bg-destructive/5 text-destructive'
  if (
    [
      'pending',
      'building',
      'creating',
      'pending_deletion',
      'deleting',
      'reconfirmation_required',
    ].includes(state ?? '')
  )
    return 'border-warning/20 bg-warning/5 text-warning'
  return 'border-border bg-muted/40 text-muted-foreground'
}

export function runnerStateLabel(state: string | null | undefined): string {
  return state ? state.replace(/_/g, ' ') : 'unknown'
}
