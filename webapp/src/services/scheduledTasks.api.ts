/** Personal scheduled-task REST API. */
import { del, get, patch, post } from './api'
import type {
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskPatch,
  ScheduledTaskRun,
} from '@opencuria/chat-core/scheduledTasks'

export type {
  ScheduleRecurrence,
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskPatch,
  ScheduledTaskRun,
} from '@opencuria/chat-core/scheduledTasks'

export function listScheduledTasks(): Promise<ScheduledTask[]> {
  return get<ScheduledTask[]>('/scheduled-tasks/')
}

export function createScheduledTask(data: ScheduledTaskInput): Promise<ScheduledTask> {
  return post<ScheduledTask>('/scheduled-tasks/', data)
}

export function updateScheduledTask(id: string, data: ScheduledTaskPatch): Promise<ScheduledTask> {
  return patch<ScheduledTask>(`/scheduled-tasks/${id}/`, data)
}

export function deleteScheduledTask(id: string): Promise<void> {
  return del<void>(`/scheduled-tasks/${id}/`)
}

export function runScheduledTaskNow(id: string): Promise<ScheduledTaskRun> {
  return post<ScheduledTaskRun>(`/scheduled-tasks/${id}/run/`)
}

export function listScheduledTaskRuns(id: string): Promise<ScheduledTaskRun[]> {
  return get<ScheduledTaskRun[]>(`/scheduled-tasks/${id}/runs/`)
}
