import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useFileExplorerStore } from './fileExplorer'
import { useEditorStore } from './editor'
import { downloadWorkspaceFile, listWorkspaceFiles, findWorkspaceFiles } from '@/services/socket'
import { readEditorFile } from '@/services/editor.api'
import { flushPromises } from '@vue/test-utils'
import { toast } from 'vue-sonner'
import type { FileContentResult, FileDownloadResult } from '@opencuria/client/types'

vi.mock('@/services/socket', () => ({
  listWorkspaceFiles: vi.fn(),
  findWorkspaceFiles: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
  sendFilesUpload: vi.fn(),
}))

vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(),
  saveEditorFile: vi.fn(),
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
  vi.mocked(listWorkspaceFiles).mockResolvedValue({
    workspace_id: WS_ID,
    request_id: 'list',
    path: '/workspace',
    entries: [],
  })
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

describe('fileExplorer directory and search state', () => {
  it('keeps isolated listings out of the tree and applies explorer listings', async () => {
    const entries = [
      { name: 'task.txt', path: '/workspace/task.txt', type: 'file' as const, size: 4 },
    ]
    vi.mocked(listWorkspaceFiles).mockResolvedValue({
      workspace_id: WS_ID,
      request_id: 'list',
      path: '/workspace',
      entries,
    })
    const store = useFileExplorerStore()
    await expect(store.fetchDirectoryEntries(WS_ID, '/workspace')).resolves.toEqual(entries)
    expect(store.tree).toEqual([])
    await store.fetchDirectory(WS_ID, '/workspace')
    expect(store.tree).toMatchObject(entries)
    expect(store.loadingPaths.size).toBe(0)
  })

  it('settles failed listings and searches without leaving loading state', async () => {
    vi.mocked(listWorkspaceFiles).mockRejectedValue(new Error('Permission denied'))
    vi.mocked(findWorkspaceFiles).mockRejectedValue(new Error('Disconnected'))
    const store = useFileExplorerStore()
    await expect(store.fetchDirectoryEntries(WS_ID, '/workspace')).resolves.toBeNull()
    await store.fetchDirectory(WS_ID, '/workspace')
    expect(store.loadingPaths.size).toBe(0)
    await expect(store.findFiles(WS_ID, 'task')).resolves.toEqual([])
  })

  it('forwards caller cancellation and discards responses arriving after reset', async () => {
    const listing = deferred<Awaited<ReturnType<typeof listWorkspaceFiles>>>()
    vi.mocked(listWorkspaceFiles).mockReturnValue(listing.promise)
    const store = useFileExplorerStore()
    const controller = new AbortController()
    const isolated = store.fetchDirectoryEntries(WS_ID, '/workspace', { signal: controller.signal })
    const signal = vi.mocked(listWorkspaceFiles).mock.calls[0]![2]!
    controller.abort()
    expect(signal.aborted).toBe(true)
    const explorer = store.fetchDirectory(WS_ID, '/workspace')
    store.reset()
    expect(vi.mocked(listWorkspaceFiles).mock.calls[1]![2]!.aborted).toBe(true)
    listing.resolve({
      workspace_id: WS_ID,
      request_id: 'list',
      path: '/workspace',
      entries: [{ name: 'stale.txt', path: '/workspace/stale.txt', type: 'file', size: 1 }],
    })
    await expect(isolated).resolves.toBeNull()
    await explorer
    expect(store.tree).toEqual([])
  })
})

describe('fileExplorer tracked upload cancellation', () => {
  it('deduplicates terminal upload results until TTL expiry and refreshes once', async () => {
    const store = useFileExplorerStore()
    const refresh = vi.mocked(listWorkspaceFiles)
    const upload = store.trackAndUpload(WS_ID, 'upload-once', '/workspace', 'once.txt', 'eA==')

    expect(
      store.handleUploadResult('unknown-from-other-client', '/workspace', 'success', WS_ID),
    ).toBe(false)
    expect(refresh).not.toHaveBeenCalled()
    vi.mocked(listWorkspaceFiles).mockClear()
    expect(store.handleUploadResult('upload-once', '/workspace', 'success', WS_ID)).toBe(true)
    await upload
    expect(store.handleUploadResult('upload-once', '/workspace', 'success', WS_ID)).toBe(false)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledWith(WS_ID, '/workspace', expect.any(AbortSignal))
  })

  it('deduplicates errors and keeps cancelled tombstones through repeated late results', async () => {
    const { toast } = await import('vue-sonner')
    const store = useFileExplorerStore()
    const refresh = vi.mocked(listWorkspaceFiles)
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
    expect(vi.mocked(listWorkspaceFiles)).not.toHaveBeenCalled()
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()

    store.cancelUpload('upload-other')
    await otherRejected
    store.reset()
  })
})

describe('fileExplorer editor integration and downloads', () => {
  it('opens selected files in the editor', async () => {
    vi.mocked(readEditorFile).mockResolvedValueOnce({
      path: PATH_A,
      content: 'hello',
      revision: 'v1',
    })
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    await flushPromises()
    expect(store.selectedPath).toBe(PATH_A)
    expect(readEditorFile).toHaveBeenCalledExactlyOnceWith(WS_ID, PATH_A, expect.any(AbortSignal))
    expect(useEditorStore().activeTab).toMatchObject({ path: PATH_A, content: 'hello' })
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

  it('resets the explorer and cancels downloads while preserving open editor tabs', async () => {
    const read = deferred<Awaited<ReturnType<typeof readEditorFile>>>()
    const download = deferred<FileDownloadResult>()
    vi.mocked(readEditorFile).mockReturnValueOnce(read.promise)
    vi.mocked(downloadWorkspaceFile).mockReturnValueOnce(download.promise)
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const pendingDownload = store.downloadFile(WS_ID, PATH_A)
    const readSignal = vi.mocked(readEditorFile).mock.calls[0]![2]!
    const downloadSignal = vi.mocked(downloadWorkspaceFile).mock.calls[0]![2]!
    store.reset()
    expect(readSignal.aborted).toBe(false)
    expect(downloadSignal.aborted).toBe(true)
    read.resolve({ path: PATH_A, content: 'open editor', revision: 'v1' })
    download.resolve({ ...readResult(), filename: 'a.txt', is_archive: false })
    await pendingDownload
    await flushPromises()
    expect(store.selectedPath).toBeNull()
    expect(useEditorStore().activeTab!.content).toBe('open editor')
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('reports a rejected download once without creating a browser URL', async () => {
    vi.mocked(downloadWorkspaceFile).mockRejectedValueOnce(new Error('Invalid file payload size'))
    await useFileExplorerStore().downloadFile(WS_ID, PATH_A)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledOnce()
  })
})
