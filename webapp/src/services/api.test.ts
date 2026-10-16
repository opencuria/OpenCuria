import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError, get, post, postDuringClipboardGesture } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
})

describe('synchronous clipboard-gesture requests', () => {
  const context = window

  it('uses the supplied frame realm synchronously with auth, organization and exact JSON', () => {
    localStorage.setItem('kern_access_token', 'access-token')
    localStorage.setItem('kern_active_org_id', 'organization-7')
    const xhr = {
      status: 200,
      responseText: '{"text":"snowman ☃ and 漢字"}',
      open: vi.fn(),
      setRequestHeader: vi.fn(),
      send: vi.fn(),
    }
    const FrameXHR = vi.fn(function XMLHttpRequest() {
      return xhr
    })
    const frame = { XMLHttpRequest: FrameXHR } as unknown as Window
    const fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)

    const result = postDuringClipboardGesture<{ text: string }>(
      '/workspaces/ws-1/desktop/clipboard/read/',
      { copy_selection: true, shift: true },
      frame,
    )

    expect(result).toEqual({ text: 'snowman ☃ and 漢字' })
    expect(FrameXHR).toHaveBeenCalledOnce()
    expect(xhr.open).toHaveBeenCalledOnce()
    expect(xhr.open).toHaveBeenCalledWith(
      'POST',
      '/api/v1/workspaces/ws-1/desktop/clipboard/read/',
      false,
    )
    expect(xhr.setRequestHeader.mock.calls).toEqual([
      ['Content-Type', 'application/json'],
      ['Authorization', 'Bearer access-token'],
      ['X-Organization-Id', 'organization-7'],
    ])
    expect(xhr.send).toHaveBeenCalledOnce()
    expect(xhr.send).toHaveBeenCalledWith('{"copy_selection":true,"shift":true}')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each([
    ['denied', 401, '{"detail":"private error"}', undefined],
    ['HTTP failure', 503, '{"detail":"private error"}', undefined],
    ['network failure', 0, '', new Error('network internals')],
    ['malformed response', 200, '{not json', undefined],
  ])('does not retry after %s', (_kind, status, responseText, sendError) => {
    localStorage.setItem('kern_refresh_token', 'refresh-token')
    const xhr = {
      status,
      responseText,
      open: vi.fn(),
      setRequestHeader: vi.fn(),
      send: sendError
        ? vi.fn(() => {
            throw sendError
          })
        : vi.fn(),
    }
    const FrameXHR = vi.fn(function XMLHttpRequest() {
      return xhr
    })
    const frame = { XMLHttpRequest: FrameXHR } as unknown as Window
    const fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)

    if (_kind === 'denied' || _kind === 'HTTP failure') {
      expect(() =>
        postDuringClipboardGesture(
          '/workspaces/ws-1/desktop/clipboard/read/',
          { copy_selection: true },
          frame,
        ),
      ).toThrow(ApiRequestError)
    } else {
      expect(() =>
        postDuringClipboardGesture(
          '/workspaces/ws-1/desktop/clipboard/read/',
          { copy_selection: true },
          frame,
        ),
      ).toThrow()
    }
    expect(FrameXHR).toHaveBeenCalledOnce()
    expect(xhr.send).toHaveBeenCalledOnce()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(localStorage.getItem('kern_refresh_token')).toBe('refresh-token')
  })
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

it('clears question drafts on confirmed authentication expiry', async () => {
  const location = { href: 'http://localhost/workspaces' }
  const storage = window.sessionStorage
  vi.stubGlobal('window', { location, sessionStorage: storage })
  sessionStorage.setItem('question-input:test', 'private draft')
  sessionStorage.setItem('chat-input-test', 'unrelated draft')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 401 }))

  await expect(get('/ordinary/')).rejects.toMatchObject({ status: 401 })

  expect(sessionStorage.getItem('question-input:test')).toBeNull()
  expect(sessionStorage.getItem('chat-input-test')).toBe('unrelated draft')
  expect(location.href).toBe('/login')
})
