import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, patch, post } from './api'
import {
  createWorkspaceFromCapturedImage,
  getDesktopStatus,
  listCapturedImages,
  recreateWorkspace,
  renameCapturedImage,
  renewDesktop,
  startDesktop,
  stopDesktop,
} from './workspaces.api'

vi.mock('./api', () => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() }))

beforeEach(() => vi.clearAllMocks())
describe('desktop viewer transport', () => {
  const intent = { viewer_client_id: 'client-id', intent_revision: 7 }
  it('sends the same viewer intent on start, stop and renew', () => {
    startDesktop('ws', intent)
    stopDesktop('ws', intent)
    renewDesktop('ws', intent)
    expect(post).toHaveBeenNthCalledWith(1, '/workspaces/ws/desktop/', intent, undefined, undefined)
    expect(post).toHaveBeenNthCalledWith(2, '/workspaces/ws/desktop/stop/', intent)
    expect(post).toHaveBeenNthCalledWith(
      3,
      '/workspaces/ws/desktop/renew/',
      intent,
      undefined,
      undefined,
    )
  })
  it('passes request cancellation through desktop transport helpers', () => {
    const controller = new AbortController()
    startDesktop('ws', intent, controller.signal)
    renewDesktop('ws', intent, controller.signal)
    getDesktopStatus('ws', intent.viewer_client_id, controller.signal)

    expect(post).toHaveBeenNthCalledWith(
      1,
      '/workspaces/ws/desktop/',
      intent,
      undefined,
      controller.signal,
    )
    expect(post).toHaveBeenNthCalledWith(
      2,
      '/workspaces/ws/desktop/renew/',
      intent,
      undefined,
      controller.signal,
    )
    expect(get).toHaveBeenNthCalledWith(
      1,
      '/workspaces/ws/desktop/status/?viewer_client_id=client-id',
      controller.signal,
    )
  })
  it('associates own status but allows unaffiliated observers', () => {
    getDesktopStatus('ws', intent.viewer_client_id)
    getDesktopStatus('ws')
    expect(get).toHaveBeenNthCalledWith(
      1,
      '/workspaces/ws/desktop/status/?viewer_client_id=client-id',
      undefined,
    )
    expect(get).toHaveBeenNthCalledWith(2, '/workspaces/ws/desktop/status/', undefined)
  })
})

describe('versioned images transport', () => {
  it('recreates a workspace on an explicit version', () => {
    recreateWorkspace('ws', 'v3')
    expect(post).toHaveBeenCalledWith('/workspaces/ws/recreate/', { image_id: 'v3' })
  })
  it('lists, renames and creates workspaces from captured images', () => {
    listCapturedImages()
    renameCapturedImage('line', 'Renamed')
    createWorkspaceFromCapturedImage('line', { name: 'feature-x' })
    expect(get).toHaveBeenCalledWith('/captured-images/')
    expect(patch).toHaveBeenCalledWith('/captured-images/line/', { name: 'Renamed' })
    expect(post).toHaveBeenCalledWith('/captured-images/line/workspaces/', { name: 'feature-x' })
  })
})
