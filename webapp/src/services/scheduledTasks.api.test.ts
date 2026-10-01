import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import {
  createScheduledTask,
  deleteScheduledTask,
  listScheduledTaskRuns,
  listScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
} from './scheduledTasks.api'

const taskInput = {
  name: 'Weekday review', workspace_id: 'ws-1', prompt: 'Review open issues', mode: 'plan' as const,
  model: '', reasoning_effort: '', skill_ids: [], recurrence: 'weekly' as const,
  weekdays: [0, 1, 2, 3, 4], local_time: '09:30', timezone_name: 'Europe/Berlin', enabled: true,
}

afterEach(() => vi.restoreAllMocks())

describe('scheduled tasks API', () => {
  it('uses the scheduled-task endpoints and sends schedule fields without creating a chat', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue([])
    await expect(listScheduledTasks()).resolves.toEqual([])
    expect(get).toHaveBeenCalledWith('/scheduled-tasks/')
    get.mockRestore()

    const post = vi.spyOn(api, 'post').mockResolvedValue({ id: 'task-1' })
    await expect(createScheduledTask(taskInput)).resolves.toEqual({ id: 'task-1' })
    expect(post).toHaveBeenCalledExactlyOnceWith('/scheduled-tasks/', taskInput)
    expect(post).not.toHaveBeenCalledWith(expect.stringContaining('sessions'), expect.anything())
  })

  it('supports patch, manual run, history and delete actions', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ id: 'task-1' })
    await expect(updateScheduledTask('task-1', { enabled: false })).resolves.toEqual({ id: 'task-1' })
    expect(patch).toHaveBeenCalledWith('/scheduled-tasks/task-1/', { enabled: false })
    patch.mockRestore()

    const post = vi.spyOn(api, 'post').mockResolvedValue({ status: 'running' })
    await expect(runScheduledTaskNow('task-1')).resolves.toEqual({ status: 'running' })
    expect(post).toHaveBeenCalledWith('/scheduled-tasks/task-1/run/')
    post.mockRestore()

    const get = vi.spyOn(api, 'get').mockResolvedValue([])
    await expect(listScheduledTaskRuns('task-1')).resolves.toEqual([])
    expect(get).toHaveBeenCalledWith('/scheduled-tasks/task-1/runs/')
    get.mockRestore()

    const del = vi.spyOn(api, 'del').mockResolvedValue(undefined)
    await expect(deleteScheduledTask('task-1')).resolves.toBeUndefined()
    expect(del).toHaveBeenCalledWith('/scheduled-tasks/task-1/')
  })
})
