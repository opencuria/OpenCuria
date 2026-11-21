import { afterEach, describe, expect, it, vi } from 'vitest'

import { getApiClient } from './api'
import { startDownload, workspaceFiles } from './workspaceFiles'

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('workspace files service', () => {
  it('shares one API that signs links through the REST client', async () => {
    const api = getApiClient()
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      files: [
        {
          path: '/workspace/a.png',
          name: 'a.png',
          type: 'file',
          size: 3,
          modified_at: 1,
          kind: 'image',
          mime_type: 'image/png',
          content_url: '/workspace-files/content/a.png',
          download_url: '/workspace-files/download/a.png',
          thumbnail_url: '',
          error: '',
        },
      ],
      expires_at: 4_102_444_800,
    })

    expect(workspaceFiles()).toBe(workspaceFiles())
    const link = await workspaceFiles().link('ws-1', '/workspace/a.png')

    expect(post).toHaveBeenCalledWith('/workspaces/ws-1/files/links/', {
      paths: ['/workspace/a.png'],
    })
    expect(link.contentUrl).toBe(`${api.getContext().baseUrl}/workspace-files/content/a.png`)
  })

  it('downloads a signed URL through a temporary anchor without buffering it', () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      expect(this.href).toBe('https://files.test/download/report.pdf')
      expect(this.getAttribute('download')).toBe('')
      expect(this.isConnected).toBe(true)
    })

    startDownload('https://files.test/download/report.pdf')

    expect(click).toHaveBeenCalledOnce()
    expect(document.querySelector('a')).toBeNull()
  })
})
