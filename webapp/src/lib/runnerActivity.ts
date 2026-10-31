import type { RunnerActivity } from '@/types/runnerStorage'

/** Counts and lists share the server's workflow grouping and resolution state. */
export function runnerActivityGroups(activities: RunnerActivity[] = []) {
  return {
    attention: activities.filter((activity) => activity.state === 'attention'),
    active: activities.filter((activity) => activity.state === 'active'),
    history: activities.filter((activity) => activity.state === 'history'),
  }
}

export function runnerActivityLabel(activity: RunnerActivity): string {
  if (activity.state === 'attention') return 'Needs attention'
  if (activity.state === 'active')
    return activity.outcome === 'recovering' ? 'Recovering' : 'In progress'
  return (
    { failed: 'Failed', resolved: 'Handled', cancelled: 'Cancelled', completed: 'Completed' }[
      activity.outcome
    ] ?? 'Finished'
  )
}
