import type { HarnessConversation } from '@/types/harness'

export function isHarnessConversationRunning(conv: HarnessConversation): boolean {
  return conv.status === 'busy'
}

export function isHarnessConversationDoneUnread(conv: HarnessConversation): boolean {
  return conv.status === 'idle' && conv.unread
}

export function isHarnessConversationAvailable(conv: HarnessConversation): boolean {
  return conv.status === 'idle' && !conv.unread
}

export function harnessConversationPreview(conv: HarnessConversation): string {
  return conv.title.trim() || 'No messages yet'
}

export function harnessConversationModeLabel(conv: HarnessConversation): string {
  return conv.mode === 'plan' ? 'Plan' : 'Build'
}
