import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useFileExplorerStore } from './fileExplorer'
import { useEditorStore } from './editor'
import { readWorkspaceFile, downloadWorkspaceFile, sendFilesList } from '@/services/socket'
import { readEditorFile } from '@/services/editor.api'
import { ApiRequestError } from '@/services/api'
import { flushPromises } from '@vue/test-utils'
import { toast } from 'vue-sonner'
import type { FileContentResult, FileDownloadResult } from '@opencuria/client/types'

vi.mock('@/services/socket', () => ({
  sendFilesList: vi.fn(),
  sendFilesFind: vi.fn(),
  readWorkspaceFile: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
  sendFilesUpload: vi.fn(),
}))

vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(),
  saveEditorFile: vi.fn(),
  readGitFileContent: vi.fn(),
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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function readResult(content = 'hello'): FileContentResult {
  return {
    workspace_id: WS_ID,
    request_id: 'read',
    path: PATH_A,
    content: btoa(content),
    size: content.length,
    truncated: false,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(readEditorFile).mockReset()
  vi.useRealTimers()
  // jsdom has no object-URL support.
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
    useEditorStore().reset()
  } catch {
    // ignore — pinia already torn down
  }
})

describe('fileExplorer correlated directory listings', () => {
  it('returns correlated entries without mutating the shared explorer tree', async () => {
    const store = useFileExplorerStore()
    const result = store.fetchDirectoryEntries(WS_ID, '/workspace')
    const requestId =
      vi.mocked(sendFilesList).mock.calls[vi.mocked(sendFilesList).mock.calls.length - 1]![1]
    const entries = [
      { name: 'task.txt', path: '/workspace/task.txt', type: 'file' as const, size: 4 },
    ]

    store.handleListResult(requestId, '/workspace', entries, undefined, 'ws-other')
    expect(store.tree).toEqual([])
    store.handleListResult(requestId, '/workspace', entries, undefined, WS_ID)

    await expect(result).resolves.toEqual(entries)
    expect(store.tree).toEqual([])
  })

  it('fails closed on directory permission errors and clears pending state on abort', async () => {
    const store = useFileExplorerStore()
    const failed = store.fetchDirectoryEntries(WS_ID, '/workspace/.opencuria')
    const failedId =
      vi.mocked(sendFilesList).mock.calls[vi.mocked(sendFilesList).mock.calls.length - 1]![1]
    store.handleListResult(failedId, '/workspace/.opencuria', [], 'Permission denied', WS_ID)
    await expect(failed).resolves.toBeNull()

    const controller = new AbortController()
    const aborted = store.fetchDirectoryEntries(WS_ID, '/workspace', { signal: controller.signal })
    controller.abort()
    await expect(aborted).resolves.toBeNull()
  })

  it('resolves timed out listings as unavailable and ignores late results', async () => {
    vi.useFakeTimers()
    try {
      const store = useFileExplorerStore()
      const result = store.fetchDirectoryEntries(WS_ID, '/workspace')
      const requestId =
        vi.mocked(sendFilesList).mock.calls[vi.mocked(sendFilesList).mock.calls.length - 1]![1]
      vi.advanceTimersByTime(30_001)
      await expect(result).resolves.toBeNull()
      store.handleListResult(requestId, '/workspace', [], undefined, WS_ID)
      expect(store.tree).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('fileExplorer tracked upload cancellation', () => {
  it('deduplicates terminal upload results until TTL expiry and refreshes once', async () => {
    const store = useFileExplorerStore()
    const refresh = vi.mocked(sendFilesList)
    const upload = store.trackAndUpload(WS_ID, 'upload-once', '/workspace', 'once.txt', 'eA==')

    expect(
      store.handleUploadResult('unknown-from-other-client', '/workspace', 'success', WS_ID),
    ).toBe(false)
    expect(refresh).not.toHaveBeenCalled()
    vi.mocked(sendFilesList).mockClear()
    expect(store.handleUploadResult('upload-once', '/workspace', 'success', WS_ID)).toBe(true)
    await upload
    expect(store.handleUploadResult('upload-once', '/workspace', 'success', WS_ID)).toBe(false)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledWith(WS_ID, expect.any(String), '/workspace')
  })

  it('deduplicates errors and keeps cancelled tombstones through repeated late results', async () => {
    const { toast } = await import('vue-sonner')
    const store = useFileExplorerStore()
    const refresh = vi.mocked(sendFilesList)
    refresh.mockClear()
    const failed = store.trackAndUpload(
      WS_ID,
      'upload-failed-once',
      '/workspace',
      'failed.txt',
      'eA==',
    )
    const rejected = expect(failed).rejects.toThrow('Disk full')
    expect(
      store.handleUploadResult('upload-failed-once', '/workspace', 'error', WS_ID, 'Disk full'),
    ).toBe(true)
    await rejected
    expect(
      store.handleUploadResult('upload-failed-once', '/workspace', 'error', WS_ID, 'Disk full'),
    ).toBe(false)

    const cancelled = store.trackAndUpload(
      WS_ID,
      'upload-cancelled-twice',
      '/workspace',
      'cancel.txt',
      'eA==',
    )
    const cancelledRejected = expect(cancelled).rejects.toThrow('Upload cancelled.')
    store.cancelUpload('upload-cancelled-twice')
    await cancelledRejected
    expect(
      store.handleUploadResult(
        'upload-cancelled-twice',
        '/workspace',
        'error',
        WS_ID,
        'late error',
      ),
    ).toBe(false)
    expect(
      store.handleUploadResult(
        'upload-cancelled-twice',
        '/workspace',
        'error',
        WS_ID,
        'late error',
      ),
    ).toBe(false)

    expect(refresh).not.toHaveBeenCalled()
    expect(vi.mocked(toast.error)).toHaveBeenCalledTimes(1)
  })

  it('cancels only the requested upload silently and ignores its late result', async () => {
    const { toast } = await import('vue-sonner')
    const store = useFileExplorerStore()
    const otherUpload = store.trackAndUpload(
      WS_ID,
      'upload-other',
      '/workspace',
      'other.txt',
      'eA==',
    )
    const cancelledUpload = store.trackAndUpload(
      WS_ID,
      'upload-owned',
      '/workspace',
      'owned.txt',
      'eA==',
      false,
    )
    const otherRejected = expect(otherUpload).rejects.toThrow('Upload cancelled.')
    const cancelledRejected = expect(cancelledUpload).rejects.toThrow('Upload cancelled.')

    store.cancelUpload('upload-owned')
    await cancelledRejected
    expect(store.shouldRefreshUploadResult('upload-owned')).toBe(true)
    expect(store.shouldRefreshUploadResult('upload-other')).toBe(true)
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()

    store.handleUploadResult('upload-owned', '/workspace', 'success', WS_ID)
    expect(vi.mocked(sendFilesList)).not.toHaveBeenCalled()
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()

    store.cancelUpload('upload-other')
    await otherRejected
    store.reset()
  })
})

describe('fileExplorer editor integration and downloads', () => {
  it('renders truncated previews with the original size and MIME type', async () => {
    const store = useFileExplorerStore()
    vi.mocked(readEditorFile).mockRejectedValueOnce(
      new ApiRequestError(400, 'Too large', 'file_too_large'),
    )
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
      ...readResult(),
      size: 10_000_000,
      truncated: true,
      mime_type: 'text/markdown',
    })
    store.selectFile(PATH_A, WS_ID)
    await flushPromises()
    expect(useEditorStore().activeTab).toMatchObject({
      path: PATH_A,
      content: 'hello',
      size: 10_000_000,
      truncated: true,
      mimeType: 'text/markdown',
      readOnly: true,
    })
    expect(useEditorStore().activeTab!.loading).toBe(false)
    expect(useEditorStore().activeTab!.error).toBeNull()
  })

  it('keeps independent tab reads without letting a late result change the active file', async () => {
    const first = deferred<Awaited<ReturnType<typeof readEditorFile>>>()
    const second = deferred<Awaited<ReturnType<typeof readEditorFile>>>()
    vi.mocked(readEditorFile).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const store = useFileExplorerStore()
    const editor = useEditorStore()
    store.selectFile(PATH_A, WS_ID)
    const firstId = editor.activeTab!.id
    const signal = vi.mocked(readEditorFile).mock.calls[0]![2]!
    store.selectFile(PATH_B, WS_ID)
    expect(signal.aborted).toBe(false)
    first.resolve({ path: PATH_A, content: 'first file', revision: 'first' })
    await flushPromises()
    expect(editor.activeTab!.path).toBe(PATH_B)
    expect(editor.activeTab!.loading).toBe(true)
    second.resolve({ path: PATH_B, content: 'second file', revision: 'second' })
    await flushPromises()
    expect(editor.activeTab).toMatchObject({ path: PATH_B, content: 'second file' })
    editor.activateTab(firstId)
    expect(editor.activeTab!.content).toBe('first file')
  })

  it('exposes editor read errors and retries the selected tab', async () => {
    vi.mocked(readEditorFile)
      .mockRejectedValueOnce(new Error('Permission denied'))
      .mockResolvedValueOnce({ path: PATH_A, content: 'hello', revision: 'v1' })
    const store = useFileExplorerStore()
    const editor = useEditorStore()
    store.selectFile(PATH_A, WS_ID)
    await flushPromises()
    expect(editor.activeTab!.error).toBe('Permission denied')
    expect(editor.activeTab!.loading).toBe(false)
    await editor.reloadTab(editor.activeTab!.id)
    expect(editor.activeTab!.content).toBe('hello')
    expect(editor.activeTab!.error).toBeNull()
  })

  it('saves a validated archive and revokes its URL after the browser click', async () => {
    vi.useFakeTimers()
    vi.mocked(downloadWorkspaceFile).mockResolvedValueOnce({
      ...readResult(),
      filename: 'workspace.tar.gz',
      is_archive: true,
    })
    await useFileExplorerStore().downloadFile(WS_ID, '/workspace')
    const blob = vi.mocked(URL.createObjectURL).mock.calls[0]![0] as Blob
    expect(blob.size).toBe(5)
    expect(blob.type).toBe('application/gzip')
    const anchor = vi.mocked(HTMLAnchorElement.prototype.click).mock
      .contexts[0] as HTMLAnchorElement
    expect(anchor.download).toBe('workspace.tar.gz')
    expect(document.body.contains(anchor)).toBe(false)
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
  })

  it.each(['closeFileViewer', 'reset'] as const)(
    '%s cancels downloads while preserving open editor tabs',
    async (action) => {
      const read = deferred<Awaited<ReturnType<typeof readEditorFile>>>()
      const download = deferred<FileDownloadResult>()
      vi.mocked(readEditorFile).mockReturnValueOnce(read.promise)
      vi.mocked(downloadWorkspaceFile).mockReturnValueOnce(download.promise)
      const store = useFileExplorerStore()
      store.selectFile(PATH_A, WS_ID)
      const pendingDownload = store.downloadFile(WS_ID, PATH_A)
      const readSignal = vi.mocked(readEditorFile).mock.calls[0]![2]!
      const downloadSignal = vi.mocked(downloadWorkspaceFile).mock.calls[0]![2]!
      store[action]()
      expect(readSignal.aborted).toBe(false)
      expect(downloadSignal.aborted).toBe(true)
      read.resolve({ path: PATH_A, content: 'open editor', revision: 'v1' })
      download.resolve({ ...readResult(), filename: 'a.txt', is_archive: false })
      await pendingDownload
      await flushPromises()
      expect(store.viewingFile).toBeNull()
      expect(store.isLoadingContent).toBe(false)
      expect(store.contentError).toBeNull()
      expect(useEditorStore().activeTab!.content).toBe('open editor')
      expect(URL.createObjectURL).not.toHaveBeenCalled()
      expect(toast.error).not.toHaveBeenCalled()
    },
  )

  it('reports a rejected download once without creating a browser URL', async () => {
    vi.mocked(downloadWorkspaceFile).mockRejectedValueOnce(new Error('Invalid file payload size'))
    await useFileExplorerStore().downloadFile(WS_ID, PATH_A)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledOnce()
  })
})
