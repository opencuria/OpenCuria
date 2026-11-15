import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, put } from './api'
import { readEditorFile, saveEditorFile } from './editor.api'

vi.mock('./api', () => ({ get: vi.fn(), put: vi.fn() }))

beforeEach(() => vi.clearAllMocks())

describe('editor API', () => {
  it('encodes file paths and forwards cancellation on complete reads', async () => {
    const signal = new AbortController().signal
    await readEditorFile('workspace/id', '/workspace/hello # ?.txt', signal)
    expect(get).toHaveBeenCalledWith(
      '/workspaces/workspace%2Fid/files/content/?path=%2Fworkspace%2Fhello+%23+%3F.txt',
      signal,
    )
  })

  it('requires the loaded revision when saving exact UTF-8 content', async () => {
    await saveEditorFile('ws', '/workspace/a.txt', 'Ä\r\nnext', 'sha256-before')
    expect(put).toHaveBeenCalledWith('/workspaces/ws/files/content/', {
      path: '/workspace/a.txt',
      content: 'Ä\r\nnext',
      expected_revision: 'sha256-before',
    })
  })
})
