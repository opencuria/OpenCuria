import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useFileExplorerStore } from './fileExplorer'
import { useEditorStore } from './editor'
import { listWorkspaceFiles, findWorkspaceFiles } from '@/services/socket'
import { readEditorFile } from '@/services/editor.api'
import {
  makeFileLink,
  resetWorkspaceFiles,
  startDownload,
  workspaceFiles,
} from '@/services/workspaceFiles.fixtures'
import { FileTransferError } from '@opencuria/client/files'
import type { WorkspaceFileLink } from '@opencuria/chat-core/workspaceFiles'
import { flushPromises } from '@vue/test-utils'
import { toast } from 'vue-sonner'

vi.mock('@/services/socket', () => ({
  listWorkspaceFiles: vi.fn(),
  findWorkspaceFiles: vi.fn(),
}))
vi.mock('@/services/workspaceFiles', () => import('@/services/workspaceFiles.fixtures'))

const files = workspaceFiles()

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

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  resetWorkspaceFiles()
  vi.mocked(readEditorFile).mockReset()
  vi.mocked(listWorkspaceFiles).mockResolvedValue({
    workspace_id: WS_ID,
    request_id: 'list',
    path: '/workspace',
    entries: [],
  })
  vi.useRealTimers()
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

describe('fileExplorer uploads', () => {
  it('uploads over HTTP and refreshes the target directory', async () => {
    const store = useFileExplorerStore()
    const body = new File(['hello'], 'a.txt')
    await expect(
      store.uploadFile(WS_ID, {
        directory: '/workspace/docs',
        filename: 'a.txt',
        body,
        conflict: 'replace',
      }),
    ).resolves.toEqual({ path: '/workspace/docs/a.txt', size: 5 })
    expect(files.upload).toHaveBeenCalledWith(WS_ID, {
      directory: '/workspace/docs',
      filename: 'a.txt',
      body,
      conflict: 'replace',
    })
    expect(listWorkspaceFiles).toHaveBeenCalledWith(
      WS_ID,
      '/workspace/docs',
      expect.any(AbortSignal),
    )
  })

  it('reports failures once and stays silent for cancelled or isolated uploads', async () => {
    const store = useFileExplorerStore()
    const body = new File(['x'], 'b.txt')
    files.upload.mockRejectedValueOnce(new FileTransferError('Disk full', 'http_507'))
    await expect(
      store.uploadFile(WS_ID, {
        directory: '/workspace',
        filename: 'b.txt',
        body,
        conflict: 'rename',
      }),
    ).resolves.toBeNull()
    expect(toast.error).toHaveBeenCalledExactlyOnceWith(
      'Upload failed',
      expect.objectContaining({ description: 'Disk full' }),
    )

    const controller = new AbortController()
    files.upload.mockImplementationOnce(async () => {
      controller.abort()
      throw new DOMException('Aborted', 'AbortError')
    })
    await expect(
      store.uploadFile(WS_ID, {
        directory: '/workspace',
        filename: 'b.txt',
        body,
        conflict: 'rename',
        signal: controller.signal,
      }),
    ).resolves.toBeNull()

    await store.uploadFile(WS_ID, {
      directory: '/workspace',
      filename: 'b.txt',
      body,
      conflict: 'rename',
      refreshExplorer: false,
    })
    expect(toast.error).toHaveBeenCalledTimes(1)
    expect(listWorkspaceFiles).not.toHaveBeenCalled()
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

  it('downloads files and folders from their signed download URL', async () => {
    files.link.mockResolvedValueOnce(
      makeFileLink('/workspace', { type: 'directory', size: 0, contentUrl: '' }),
    )
    await useFileExplorerStore().downloadFile(WS_ID, '/workspace')
    expect(files.link).toHaveBeenCalledWith(WS_ID, '/workspace', expect.any(AbortSignal))
    expect(startDownload).toHaveBeenCalledExactlyOnceWith('https://files.test/download/workspace')

    await useFileExplorerStore().downloadFile(WS_ID, PATH_A)
    expect(startDownload).toHaveBeenLastCalledWith(`https://files.test/download${PATH_A}`)
  })

  it('refuses downloads above the transfer limit', async () => {
    files.link.mockResolvedValueOnce(makeFileLink(PATH_A, { size: 600 * 1024 * 1024 }))
    await useFileExplorerStore().downloadFile(WS_ID, PATH_A)
    expect(startDownload).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith(
      'Download failed',
      expect.objectContaining({ description: 'Files up to 500 MB can be transferred.' }),
    )
  })

  it('resets the explorer and cancels downloads while preserving open editor tabs', async () => {
    const read = deferred<Awaited<ReturnType<typeof readEditorFile>>>()
    const link = deferred<WorkspaceFileLink>()
    vi.mocked(readEditorFile).mockReturnValueOnce(read.promise)
    files.link.mockReturnValueOnce(link.promise)
    const store = useFileExplorerStore()
    store.selectFile(PATH_A, WS_ID)
    const pendingDownload = store.downloadFile(WS_ID, PATH_A)
    const readSignal = vi.mocked(readEditorFile).mock.calls[0]![2]!
    const downloadSignal = files.link.mock.calls[0]![2] as AbortSignal
    store.reset()
    expect(readSignal.aborted).toBe(false)
    expect(downloadSignal.aborted).toBe(true)
    read.resolve({ path: PATH_A, content: 'open editor', revision: 'v1' })
    link.resolve(makeFileLink(PATH_A))
    await pendingDownload
    await flushPromises()
    expect(store.selectedPath).toBeNull()
    expect(useEditorStore().activeTab!.content).toBe('open editor')
    expect(startDownload).not.toHaveBeenCalled()
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('reports a failed link once without starting a download', async () => {
    files.link.mockRejectedValueOnce(
      new FileTransferError('The file does not exist.', 'file_not_found'),
    )
    await useFileExplorerStore().downloadFile(WS_ID, PATH_A)
    expect(startDownload).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledExactlyOnceWith(
      'Download failed',
      expect.objectContaining({ description: 'The file does not exist.' }),
    )
  })
})
