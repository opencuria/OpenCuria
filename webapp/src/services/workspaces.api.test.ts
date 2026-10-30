import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, patch, post } from './api'
import {
  copyDesktopSelectionDuringGesture,
  createWorkspaceFromCapturedImage,
  getDesktopStatus,
  listCapturedImages,
  recreateWorkspace,
  renameCapturedImage,
  renewDesktop,
  startDesktop,
  stopDesktop,
} from './workspaces.api'

const apiMocks = vi.hoisted(() => ({ postDuringClipboardGesture: vi.fn() }))

const postDuringClipboardGesture = apiMocks.postDuringClipboardGesture

vi.mock('./api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  postDuringClipboardGesture: apiMocks.postDuringClipboardGesture,
  patch: vi.fn(),
  del: vi.fn(),
}))

beforeEach(() => vi.clearAllMocks())
describe('desktop viewer transport', () => {
  const intent = { viewer_client_id: 'client-id', intent_revision: 7 }
  it('sends the same viewer intent on start, stop and renew', () => {
    startDesktop('ws', intent)
    stopDesktop('ws', intent)
    renewDesktop('ws', intent)
    expect(post).toHaveBeenNthCalledWith(1, '/workspaces/ws/desktop/', intent, undefined)
    expect(post).toHaveBeenNthCalledWith(2, '/workspaces/ws/desktop/stop/', intent)
    expect(post).toHaveBeenNthCalledWith(
      3,
      '/workspaces/ws/desktop/renew/',
      intent,
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
      controller.signal,
    )
    expect(post).toHaveBeenNthCalledWith(
      2,
      '/workspaces/ws/desktop/renew/',
      intent,
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

describe('clipboard selection transport', () => {
  it('requests selection text synchronously with the exact copy intent and shift flag', () => {
    const response = { text: 'Unicode: café — 東京 🐈' }
    vi.mocked(postDuringClipboardGesture).mockReturnValueOnce(response)

    expect(copyDesktopSelectionDuringGesture('workspace/a', window, true)).toBe(response.text)
    expect(postDuringClipboardGesture).toHaveBeenCalledOnce()
    expect(postDuringClipboardGesture).toHaveBeenCalledWith(
      '/workspaces/workspace/a/desktop/clipboard/read/',
      { copy_selection: true, shift: true },
      window,
    )
  })

  it.each([undefined, null, {}, { text: 42 }, { text: null }])(
    'rejects a malformed or non-string response (%s)',
    (response) => {
      vi.mocked(postDuringClipboardGesture).mockReturnValueOnce(response as never)
      expect(() => copyDesktopSelectionDuringGesture('ws', window)).toThrow(
        'Invalid clipboard response',
      )
    },
  )
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
