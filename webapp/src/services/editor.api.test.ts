import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, put } from './api'
import { readEditorFile, readGitFileContent, saveEditorFile } from './editor.api'

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

  it('reads full Git blobs with an independently encoded source and path', async () => {
    await readGitFileContent('ws', '/workspace/repo space', 'a#b.txt', 'abc123^')
    expect(get).toHaveBeenCalledWith(
      '/workspaces/ws/git/file-content/?repo_path=%2Fworkspace%2Frepo+space&path=a%23b.txt&revision=abc123%5E',
      undefined,
    )
  })
})
