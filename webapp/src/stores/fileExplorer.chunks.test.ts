import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useFileExplorerStore } from './fileExplorer'
import { sendFilesDownload, sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesList: vi.fn(),
  sendFilesFind: vi.fn(),
  sendFilesRead: vi.fn(),
  sendFilesDownload: vi.fn(),
}))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const WS_ID = 'ws-1'
const PATH_A = '/workspace/a.txt'
const PATH_B = '/workspace/b.txt'

/** Split a base64 string into `n` pieces on 4-char boundaries (keeps atob happy). */
function splitAligned(b64: string, n: number): string[] {
  const per = Math.max(4, Math.floor(b64.length / n / 4) * 4)
  const parts: string[] = []
  for (let i = 0; i < n - 1; i++) {
    parts.push(b64.slice(i * per, (i + 1) * per))
  }
  parts.push(b64.slice((n - 1) * per))
  return parts
}

function lastReadRequestId(): string {
  const calls = vi.mocked(sendFilesRead).mock.calls
  return calls[calls.length - 1]![1]
}

function lastDownloadRequestId(): string {
  const calls = vi.mocked(sendFilesDownload).mock.calls
  return calls[calls.length - 1]![1]
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useRealTimers()
  // jsdom has no object-URL support; download assembly is asserted via these spies.
  Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:mock-url'),
    revokeObjectURL: vi.fn(),
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  try {
    useFileExplorerStore().reset()
  } catch {
    // ignore — pinia already torn down
  }
})

describe('fileExplorer chunked content reads', () => {
  it('assembles out-of-order chunks and populates viewingFile on chunked final', () => {
    const store = useFileExplorerStore()
    // fetchFileContent is private; selectFile is the public entry that arms it.
    store.selectFile(PATH_A, WS_ID)
    const req = lastReadRequestId()

    const original = 'Hello chunked world, out-of-order test.'
    const full = btoa(original)
    const [p0, p1, p2] = splitAligned(full, 3)

    store.handleContentChunk(req, PATH_A, 2, 3, p2!)
    store.handleContentChunk(req, PATH_A, 0, 3, p0!)
    store.handleContentChunk(req, PATH_A, 1, 3, p1!)
    store.handleContentResult(req, PATH_A, '', 1234, false, undefined, {
      chunked: true,
      totalChunks: 3,
    })

    expect(store.viewingFile).not.toBeNull()
    expect(store.viewingFile?.path).toBe(PATH_A)
    expect(store.viewingFile?.rawBase64).toBe(full)
    expect(store.viewingFile?.content).toBe(original)
    expect(store.isLoadingContent).toBe(false)
    expect(store.contentError).toBeNull()
  })

  it('ignores late chunks/results for a superseded click (stale A vs active B)', () => {
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const reqA = lastReadRequestId()
    store.selectFile(PATH_B, WS_ID)
    const reqB = lastReadRequestId()
    expect(reqB).not.toBe(reqA)

    // Late traffic for the cancelled read A must be dropped silently.
    store.handleContentChunk(reqA, PATH_A, 0, 1, btoa('stale-bytes'))
    store.handleContentResult(reqA, PATH_A, btoa('stale-bytes'), 11, false)
    expect(store.viewingFile).toBeNull()
    expect(store.contentError).toBeNull()
    expect(store.isLoadingContent).toBe(true)

    // The active read B still completes normally.
    const originalB = 'fresh content for B'
    const fullB = btoa(originalB)
    const [b0, b1] = splitAligned(fullB, 2)
    store.handleContentChunk(reqB, PATH_B, 1, 2, b1!)
    store.handleContentChunk(reqB, PATH_B, 0, 2, b0!)
    store.handleContentResult(reqB, PATH_B, '', originalB.length, false, undefined, {
      chunked: true,
      totalChunks: 2,
    })
    expect(store.viewingFile?.path).toBe(PATH_B)
    expect(store.viewingFile?.content).toBe(originalB)
  })

  it('sets contentError and stops loading when the read times out', () => {
    vi.useFakeTimers()
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    expect(store.isLoadingContent).toBe(true)

    vi.advanceTimersByTime(30_001)

    expect(store.isLoadingContent).toBe(false)
    expect(store.contentError).toBeTruthy()
    expect(store.viewingFile).toBeNull()
  })

  it('fails visibly on an invalid/incomplete chunked final', () => {
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const req = lastReadRequestId()

    // Only 1 of 2 announced chunks arrives, then the chunked final fires.
    const full = btoa('partial content here!!')
    const [first] = splitAligned(full, 2)
    store.handleContentChunk(req, PATH_A, 0, 2, first!)
    store.handleContentResult(req, PATH_A, '', 999, false, undefined, {
      chunked: true,
      totalChunks: 2,
    })

    expect(store.contentError).toBeTruthy()
    expect(store.viewingFile).toBeNull()
    expect(store.isLoadingContent).toBe(false)
  })
})

describe('fileExplorer chunked downloads', () => {
  it('assembles download chunks and triggers an anchor download', () => {
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    const original = 'download-bytes-1234'
    const full = btoa(original)
    const [d0, d1] = splitAligned(full, 2)
    store.handleDownloadChunk(req, PATH_A, 0, 2, d0!)
    store.handleDownloadChunk(req, PATH_A, 1, 2, d1!)
    store.handleDownloadResult(req, '', 'a.txt', false, undefined, {
      chunked: true,
      totalChunks: 2,
      size: original.length,
    })

    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1)
  })

  it('revokes the object URL delayed (not synchronously)', () => {
    vi.useFakeTimers()
    try {
      const store = useFileExplorerStore()
      store.downloadFile(WS_ID, PATH_A)
      const req = lastDownloadRequestId()

      const original = 'delayed-revoke!'
      const full = btoa(original)
      store.handleDownloadResult(req, full, 'a.txt', false, undefined, {
        size: original.length,
      })

      expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
      expect(vi.mocked(URL.revokeObjectURL)).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1000)
      expect(vi.mocked(URL.revokeObjectURL)).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('surfaces invalid base64 as Download failed without throwing', () => {
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    expect(() =>
      store.handleDownloadResult(req, '!!!not-base64!!!', 'a.txt', false, undefined, {
        size: 4,
      }),
    ).not.toThrow()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('rejects oversize size/payload before atob (no object URL)', () => {
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    const full = btoa('tiny')
    expect(() =>
      store.handleDownloadResult(req, full, 'a.txt', false, undefined, {
        size: 100 * 1024 * 1024 + 1,
      }),
    ).not.toThrow()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('rejects decoded-size mismatch against the reported size', () => {
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    // 17 raw bytes; reporting 16 must fail closed with no download.
    const full = btoa('exactly-sixteen!!')
    expect(() =>
      store.handleDownloadResult(req, full, 'a.txt', false, undefined, {
        size: 16,
      }),
    ).not.toThrow()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
})

describe('fileExplorer expected-path fail-closed', () => {
  it('fails visibly when the first chunk claims a different path', () => {
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const req = lastReadRequestId()

    store.handleContentChunk(req, PATH_B, 0, 1, btoa('evil-bytes'))

    expect(store.contentError).toBeTruthy()
    expect(store.viewingFile).toBeNull()
    expect(store.isLoadingContent).toBe(false)
  })

  it('fails visibly when the chunked final carries a different path', () => {
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const req = lastReadRequestId()

    const full = btoa('hello-path-check')
    const [p0] = splitAligned(full, 1)
    store.handleContentChunk(req, PATH_A, 0, 1, p0!)
    store.handleContentResult(req, PATH_B, '', full.length, false, undefined, {
      chunked: true,
      totalChunks: 1,
    })

    expect(store.contentError).toBeTruthy()
    expect(store.viewingFile).toBeNull()
  })

  it('clears all in-flight download state on closeFileViewer', () => {
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()
    const full = btoa('download-bytes-xyz')
    const [d0, d1] = splitAligned(full, 2)
    store.handleDownloadChunk(req, PATH_A, 0, 2, d0!)

    store.closeFileViewer()

    // Late completion for the closed transfer is ignored; no download fires.
    vi.mocked(URL.createObjectURL).mockClear()
    store.handleDownloadChunk(req, PATH_A, 1, 2, d1!)
    store.handleDownloadResult(req, '', 'a.txt', false, undefined, {
      chunked: true,
      totalChunks: 2,
    })
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('a new click cancels the older read so its late final never populates', () => {
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const reqA = lastReadRequestId()
    store.selectFile(PATH_B, WS_ID)
    const reqB = lastReadRequestId()

    store.handleContentResult(reqA, PATH_A, btoa('late-A'), 6, false)
    expect(store.viewingFile).toBeNull()

    store.handleContentResult(reqB, PATH_B, btoa('fresh-B'), 7, false)
    expect(store.viewingFile?.path).toBe(PATH_B)
  })
})

describe('fileExplorer download size integer guard', () => {
  it('rejects a non-integer expectedSize without downloading', () => {
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    const full = btoa('twelve-bytes!')
    expect(() =>
      store.handleDownloadResult(req, full, 'a.txt', false, undefined, {
        size: 12.5,
      }),
    ).not.toThrow()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('rejects a non-integer size on the chunked path with one toast', async () => {
    const { toast } = await import('vue-sonner')
    vi.mocked(toast.error).mockClear()
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    const original = 'chunked-int-guard'
    const full = btoa(original)
    const [d0, d1] = splitAligned(full, 2)
    store.handleDownloadChunk(req, PATH_A, 0, 2, d0!)
    store.handleDownloadChunk(req, PATH_A, 1, 2, d1!)
    expect(() =>
      store.handleDownloadResult(req, '', 'a.txt', false, undefined, {
        chunked: true,
        totalChunks: 2,
        size: 17.5,
      }),
    ).not.toThrow()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    // Exactly one visible failure: validateDownloadSize fails inside the
    // try (failDownloadTransfer), triggerValidatedDownload never runs.
    expect(vi.mocked(toast.error)).toHaveBeenCalledTimes(1)
  })

  it('fires a single toast when the inline trigger payload mismatches', async () => {
    const { toast } = await import('vue-sonner')
    vi.mocked(toast.error).mockClear()
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    // 4 decoded bytes reported as 5: triggerValidatedDownload fails once.
    expect(() =>
      store.handleDownloadResult(req, btoa('tiny'), 'a.txt', false, undefined, {
        size: 5,
      }),
    ).not.toThrow()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(vi.mocked(toast.error)).toHaveBeenCalledTimes(1)
  })

  it('fires a single toast for an error final (no trigger double-toast)', async () => {
    const { toast } = await import('vue-sonner')
    vi.mocked(toast.error).mockClear()
    const store = useFileExplorerStore()
    store.downloadFile(WS_ID, PATH_A)
    const req = lastDownloadRequestId()

    store.handleDownloadResult(req, '', 'a.txt', false, 'Runner exploded')
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(vi.mocked(toast.error)).toHaveBeenCalledTimes(1)
  })
})
