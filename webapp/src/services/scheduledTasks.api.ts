/** Personal scheduled-task REST API. */
import { createScheduledTasksApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export type {
  ScheduleRecurrence,
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskPatch,
  ScheduledTaskRun,
} from '@opencuria/chat-core/scheduledTasks'

export const {
  list: listScheduledTasks,
  create: createScheduledTask,
  update: updateScheduledTask,
  delete: deleteScheduledTask,
  run: runScheduledTaskNow,
  runs: listScheduledTaskRuns,
} = createScheduledTasksApi(getApiClient)
