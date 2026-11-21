/**
 * "Send later" for a chat that does not exist yet: a one-time scheduled task
 * starts it with the composer's draft at the chosen time.
 */
import { formatSendTime, sendLaterTaskInput } from '@opencuria/chat-core/sendLater'
import { useNotificationStore } from '@/stores/notifications'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'

/** The composer's draft of a new chat: everything but the time. */
export type NewChatDraft = Omit<Parameters<typeof sendLaterTaskInput>[0], 'at' | 'timeZone'>

/**
 * Schedule `draft` to start as a new chat at `at`. Resolves `true` once the
 * task exists and `false` after an error toast, so the composer can give the
 * draft back.
 */
export async function scheduleNewChat(draft: NewChatDraft, at: Date): Promise<boolean> {
  const notifications = useNotificationStore()
  try {
    await useScheduledTaskStore().create(sendLaterTaskInput({ ...draft, at }))
    notifications.success('New chat scheduled', formatSendTime(at))
    return true
  } catch (e: unknown) {
    notifications.error('Scheduling failed', e instanceof Error ? e.message : 'Unknown error')
    return false
  }
}
