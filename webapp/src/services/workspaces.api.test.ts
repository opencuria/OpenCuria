import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post } from './api'
import { getDesktopStatus, renewDesktop, startDesktop, stopDesktop } from './workspaces.api'

vi.mock('./api', () => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() }))

beforeEach(() => vi.clearAllMocks())
describe('desktop viewer transport', () => {
  const intent = { viewer_client_id: 'client-id', intent_revision: 7 }
  it('sends the same viewer intent on start, stop and renew', () => {
    startDesktop('ws', intent)
    stopDesktop('ws', intent)
    renewDesktop('ws', intent)
    expect(post).toHaveBeenNthCalledWith(1, '/workspaces/ws/desktop/', intent)
    expect(post).toHaveBeenNthCalledWith(2, '/workspaces/ws/desktop/stop/', intent)
    expect(post).toHaveBeenNthCalledWith(3, '/workspaces/ws/desktop/renew/', intent)
  })
  it('associates own status but allows unaffiliated observers', () => {
    getDesktopStatus('ws', intent.viewer_client_id)
    getDesktopStatus('ws')
    expect(get).toHaveBeenNthCalledWith(
      1,
      '/workspaces/ws/desktop/status/?viewer_client_id=client-id',
    )
    expect(get).toHaveBeenNthCalledWith(2, '/workspaces/ws/desktop/status/')
  })
})
