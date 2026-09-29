import { describe, expect, it, vi } from 'vitest'
import { get } from './api'
import { getHarnessPart, listHarnessParts } from './harness.api'

vi.mock('./api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
  requestWithStatus: vi.fn(),
}))

const getMock = vi.mocked(get)

describe('harness timeline API', () => {
  it('uses the lightweight all-message timeline endpoint', async () => {
    getMock.mockResolvedValueOnce({ session: {}, messages: [] })
    await listHarnessParts('session id')
    expect(getMock).toHaveBeenCalledWith('/harness/sessions/session id/timeline')
  })

  it('fetches details for only the requested part', async () => {
    getMock.mockResolvedValueOnce({ id: 'p1', output: 'full' })
    await getHarnessPart('s1', 'p1')
    expect(getMock).toHaveBeenCalledWith('/harness/sessions/s1/parts/p1')
  })
})
