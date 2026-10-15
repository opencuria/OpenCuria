import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from './api'
import { forkHarnessSession } from './harness.api'

vi.mock('./api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
  requestWithStatus: vi.fn(),
}))

describe('harness fork API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('sends the inclusive answer cutoff to the backend', async () => {
    await forkHarnessSession('session-1', { message_id: 'answer-1', include_message: true })
    expect(post).toHaveBeenCalledWith('/harness/sessions/session-1/fork', {
      message_id: 'answer-1',
      include_message: true,
    })
  })

  it('preserves legacy exclusive cutoffs and full-session payloads', async () => {
    await forkHarnessSession('s1', { message_id: 'user-1' })
    expect(post).toHaveBeenLastCalledWith('/harness/sessions/s1/fork', { message_id: 'user-1' })
    await forkHarnessSession('s1')
    expect(post).toHaveBeenLastCalledWith('/harness/sessions/s1/fork', {})
  })
})
