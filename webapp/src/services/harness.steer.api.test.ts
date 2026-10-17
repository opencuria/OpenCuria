import { beforeEach, describe, expect, it, vi } from 'vitest'
import { del, post } from './api'
import { steerHarnessSession, withdrawHarnessSteer } from './harness.api'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    patch: vi.fn(),
    requestWithStatus: vi.fn(),
  }
})

describe('harness steer API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('queues a message into the running turn', async () => {
    const steer = { id: 's1', content: 'Use pnpm', created_at: '2026-10-16T10:00:00Z' }
    vi.mocked(post).mockResolvedValueOnce(steer)

    await expect(steerHarnessSession('session-1', 'Use pnpm')).resolves.toEqual(steer)

    expect(post).toHaveBeenCalledWith('/harness/sessions/session-1/steer', { prompt: 'Use pnpm' })
  })

  it('withdraws a queued steer and returns the withdrawn entry', async () => {
    const steer = { id: 's1', content: 'Use pnpm', created_at: '2026-10-16T10:00:00Z' }
    vi.mocked(del).mockResolvedValueOnce(steer)

    await expect(withdrawHarnessSteer('session-1', 's1')).resolves.toEqual(steer)

    expect(del).toHaveBeenCalledWith('/harness/sessions/session-1/steer/s1')
  })
})
