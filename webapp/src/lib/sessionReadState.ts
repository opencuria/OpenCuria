import type { Session } from '@/types'
import { isSessionDone } from './sessionState'

export function isSessionEligibleForAutoRead(
  session: Session,
  suppressedSessionIds: ReadonlySet<string> = new Set(),
): boolean {
  return (
    isSessionDone(session.status) &&
    !session.read_at &&
    !suppressedSessionIds.has(session.id)
  )
}

export function findLatestUnreadDoneSession(
  sessions: Session[],
  suppressedSessionIds: ReadonlySet<string> = new Set(),
): Session | null {
  return (
    [...sessions]
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
      .find((session) => isSessionEligibleForAutoRead(session, suppressedSessionIds)) ?? null
  )
}
