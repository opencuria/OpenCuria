import { afterEach, describe, expect, it, vi } from 'vitest'
import { get, post } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('abortable requests', () => {
  it('passes a signal through initial and post-refresh retry fetches', async () => {
    localStorage.setItem('kern_refresh_token', 'refresh-token')
    const controller = new AbortController()
    const response = (status: number, data: unknown): Response =>
      ({
        ok: status >= 200 && status < 300,
        status,
        json: async () => data,
      }) as Response
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response(401, { detail: 'expired' }))
      .mockResolvedValueOnce(
        response(200, { access_token: 'new-access', refresh_token: 'new-refresh' }),
      )
      .mockResolvedValueOnce(response(200, { ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      post('/desktop/', { intent_revision: 3 }, undefined, controller.signal),
    ).resolves.toEqual({
      ok: true,
    })

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/v1/desktop/',
      expect.objectContaining({ signal: controller.signal }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/v1/auth/refresh/', expect.any(Object))
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/v1/desktop/',
      expect.objectContaining({ signal: controller.signal }),
    )
  })

  it('keeps unrelated requests usable without a signal', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(get('/ordinary/')).resolves.toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/ordinary/',
      expect.objectContaining({ signal: undefined }),
    )
  })
})
