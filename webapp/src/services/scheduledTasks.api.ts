/** Personal scheduled-task REST API. */
import { del, get, patch, post } from './api'

export interface ScheduledTask {
  id: string
  name: string
  workspace_id: string
  prompt: string
  mode: 'plan' | 'build'
  model: string
  reasoning_effort: string
  skill_ids: string[]
  recurrence: 'daily' | 'weekly'
  weekdays: number[]
  local_time: string
  timezone_name: string
  enabled: boolean
  next_run_at: string
  created_at: string
  updated_at: string
}

export interface ScheduledTaskInput {
  name: string
  workspace_id: string
  prompt: string
  mode: 'plan' | 'build'
  model: string
  reasoning_effort: string
  skill_ids: string[]
  recurrence: 'daily' | 'weekly'
  weekdays: number[]
  local_time: string
  timezone_name: string
  enabled: boolean
}

export type ScheduledTaskPatch = Partial<Omit<ScheduledTaskInput, 'workspace_id'>>

export interface ScheduledTaskRun {
  id: string
  scheduled_for: string
  status: 'claimed' | 'running' | 'succeeded' | 'error' | 'skipped' | 'interrupted' | string
  session_id: string | null
  started_at: string | null
  finished_at: string | null
  error: string
  reason: string
  assistant_finish: string
  assistant_error: string
}

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
