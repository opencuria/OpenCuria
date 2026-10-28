import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from './api'
import { createHarnessSession, listHarnessParts, sendHarnessMessage } from './harness.api'

const config = vi.hoisted(() => ({ apiBaseUrl: '/api/v1' }))
vi.mock('./config', () => ({ getConfig: () => config }))

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  config.apiBaseUrl = '/api/v1'
})

describe('web bindings for the shared harness API', () => {
  it('keeps browser credentials and resolves the current server, token and organization per call', async () => {
    localStorage.setItem('kern_access_token', 'web-token')
    localStorage.setItem('kern_active_org_id', 'org-1')
    const fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(
        async () => new Response(JSON.stringify({ id: 'session-1' }), { status: 200 }),
      )

    await createHarnessSession('workspace/id', {
      prompt: 'hello',
      harness_id: 'codex',
      model: 'chatgpt/gpt-6.1-sol',
      reasoning_effort: 'ultra',
    })
    expect(fetch).toHaveBeenLastCalledWith(
      '/api/v1/workspaces/workspace%2Fid/harness/sessions/',
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        body: JSON.stringify({
          prompt: 'hello',
          harness_id: 'codex',
          model: 'chatgpt/gpt-6.1-sol',
          reasoning_effort: 'ultra',
        }),
        headers: expect.objectContaining({
          Authorization: 'Bearer web-token',
          'X-Organization-Id': 'org-1',
        }),
      }),
    )

    config.apiBaseUrl = '/curia/api/v1'
    localStorage.setItem('kern_access_token', 'new-token')
    localStorage.setItem('kern_active_org_id', 'org-2')
    const controller = new AbortController()
    await listHarnessParts('session-1', controller.signal)
    expect(fetch).toHaveBeenLastCalledWith(
      '/curia/api/v1/harness/sessions/session-1/timeline',
      expect.objectContaining({
        method: 'GET',
        credentials: 'same-origin',
        signal: controller.signal,
        headers: expect.objectContaining({
          Authorization: 'Bearer new-token',
          'X-Organization-Id': 'org-2',
        }),
      }),
    )
  })

  it('preserves structured API failures for store and view error handling', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ detail: 'Workspace is transitioning', code: 'workspace_busy' }),
        { status: 409 },
      ),
    )
    await expect(sendHarnessMessage('session-1', { prompt: 'continue' })).rejects.toMatchObject({
      constructor: ApiRequestError,
      status: 409,
      code: 'workspace_busy',
      message: 'Workspace is transitioning',
    })
  })
})
