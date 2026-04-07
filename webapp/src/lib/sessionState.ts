import { SessionStatus, type Conversation } from '@/types'

export function isSessionActive(status: SessionStatus): boolean {
  return status === SessionStatus.PENDING || status === SessionStatus.RUNNING
}

export function isSessionDone(status: SessionStatus): boolean {
  return status === SessionStatus.COMPLETED || status === SessionStatus.FAILED
}

export function isSessionFailed(status: SessionStatus): boolean {
  return status === SessionStatus.FAILED
}

export function isConversationRunning(conv: Conversation): boolean {
  const status = conv.last_session?.status
  return status ? isSessionActive(status) : false
}

export function isConversationDoneUnread(conv: Conversation): boolean {
  const status = conv.last_session?.status
  return Boolean(status && isSessionDone(status) && !conv.is_read)
}

export function isConversationIdle(conv: Conversation): boolean {
  const status = conv.last_session?.status
  if (!status) return true
  if (isSessionActive(status)) return false
  return conv.is_read
}
