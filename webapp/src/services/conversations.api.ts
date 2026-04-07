/**
 * API client for the conversations endpoint.
 */

import type { Conversation } from '@/types'
import { get, post } from './api'

export function listConversations(): Promise<Conversation[]> {
  return get<Conversation[]>('/conversations/')
}

export function markConversationRead(sessionId: string): Promise<void> {
  return post<void>('/conversations/read/', {
    session_id: sessionId,
  })
}

export function markConversationUnread(sessionId: string): Promise<void> {
  return post<void>('/conversations/unread/', {
    session_id: sessionId,
  })
}
