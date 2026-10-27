/**
 * File explorer store — manages the file explorer panel state,
 * tree structure, and file viewing.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { FileNode, FileEntryRaw } from '@/types'
import {
  sendFilesList,
  sendFilesFind,
  readWorkspaceFile,
  downloadWorkspaceFile,
} from '@/services/socket'
import { useWorkspaceStore } from './workspaces'
import { useNotificationStore } from '@/stores/notifications'
import { useSidePanelStore } from '@/stores/sidePanel'

let requestCounter = 0

function nextRequestId(): string {
  return `files-${++requestCounter}-${Date.now()}`
}

/** Delay before a download object URL is revoked (lets the click dispatch). */
const DOWNLOAD_REVOKE_DELAY_MS = 1000

/** Keep terminal upload ids briefly so duplicate/late socket results stay silent. */
const UPLOAD_TERMINAL_TTL_MS = 30_000
/** Bound terminal upload bookkeeping if a result stream is unusually busy. */
const MAX_TERMINAL_UPLOAD_IDS = 500

/** How long a directory, search or upload request may stay pending. */
const REQUEST_TIMEOUT_MS = 30_000

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return 'unknown size'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export const useFileExplorerStore = defineStore('fileExplorer', () => {
  // -- state ----------------------------------------------------------------

  const tree = ref<FileNode[]>([])
  const expandedPaths = ref<Set<string>>(new Set())
  const selectedPath = ref<string | null>(null)
  const loadingPaths = ref<Set<string>>(new Set())

  const viewingFile = ref<{
    path: string
    content: string
    rawBase64: string
    size: number
    truncated: boolean
    previewBytes: number
    isBinary: boolean
    mediaType: 'text' | 'image' | 'pdf' | 'binary'
    mimeType: string
  } | null>(null)
  const isLoadingContent = ref(false)
  const contentError = ref<string | null>(null)

  // Pending request callbacks: request_id → resolver
  const pendingRequests = ref<Map<string, (data: unknown) => void>>(new Map())
  const pendingDirectoryRequests = new Map<
    string,
    {
      workspaceId: string
      path: string
      updateTree: boolean
      onResult: (entries: FileEntryRaw[] | null) => void
      signal?: AbortSignal
      abort?: () => void
    }
  >()
  const pendingUploadRefreshPolicy = new Map<string, boolean>()
  const pendingUploadRequests = new Map<string, { workspaceId: string; path: string }>()
  const terminalUploadRequestIds = new Map<string, ReturnType<typeof setTimeout>>()
  const pendingFindRequests = ref<Map<string, (paths: string[]) => void>>(new Map())

  let contentController: AbortController | null = null
  const downloadControllers = new Set<AbortController>()
  const pendingTimers = new Map<string, ReturnType<typeof setTimeout>>()

  function clearPendingTimer(requestId: string): void {
    const timer = pendingTimers.get(requestId)
    if (timer) {
      clearTimeout(timer)
      pendingTimers.delete(requestId)
    }
  }

  function armPendingTimer(requestId: string, onFire: () => void, ms = REQUEST_TIMEOUT_MS): void {
    clearPendingTimer(requestId)
    const timer = setTimeout(() => {
      pendingTimers.delete(requestId)
      onFire()
    }, ms)
    pendingTimers.set(requestId, timer)
  }

  // -- getters --------------------------------------------------------------

  const isViewingFile = computed(() => viewingFile.value !== null)

  // -- actions --------------------------------------------------------------

  function setTree(path: string, entries: FileEntryRaw[]): void {
    const nodes: FileNode[] = entries.map((e) => ({
      name: e.name,
      path: e.path,
      type: e.type,
      size: e.size,
      children: e.type === 'directory' ? undefined : undefined,
      isExpanded: false,
    }))

    if (path === '/workspace') {
      tree.value = nodes
    } else {
      // Find the parent node and set its children
      setChildNodes(tree.value, path, nodes)
    }
  }

  function setChildNodes(nodes: FileNode[], parentPath: string, children: FileNode[]): void {
    for (const node of nodes) {
      if (node.path === parentPath) {
        node.children = children
        return
      }
      if (node.children) {
        setChildNodes(node.children, parentPath, children)
      }
    }
  }

  function findNode(nodes: FileNode[], targetPath: string): FileNode | null {
    for (const node of nodes) {
      if (node.path === targetPath) {
        return node
      }
      if (node.children) {
        const child = findNode(node.children, targetPath)
        if (child) {
          return child
        }
      }
    }
    return null
  }

  async function ensureDirectoryLoaded(workspaceId: string, path: string): Promise<void> {
    const node = findNode(tree.value, path)
    if (path !== '/workspace' && (!node || node.type !== 'directory')) {
      return
    }
    if (path !== '/workspace' && node?.children) {
      return
    }
    await fetchDirectory(workspaceId, path)
  }

  /** Open an already-resolved filesystem path (not a URL). */
  async function openPath(
    workspaceId: string,
    path: string,
    options: { revealInExplorer?: boolean } = {},
  ): Promise<void> {
    const { revealInExplorer = true } = options
    if (path !== '/workspace' && !path.startsWith('/workspace/')) {
      return
    }

    if (revealInExplorer) {
      useSidePanelStore().open('files')
    }

    const segments = path.split('/').filter(Boolean)
    if (!segments.length || segments[0] !== 'workspace') {
      return
    }

    await ensureDirectoryLoaded(workspaceId, '/workspace')

    let currentPath = '/workspace'
    for (let i = 1; i < segments.length - 1; i++) {
      currentPath = `${currentPath}/${segments[i]}`
      expandedPaths.value.add(currentPath)
      await ensureDirectoryLoaded(workspaceId, currentPath)
    }

    const targetNode = findNode(tree.value, path)
    if (targetNode?.type === 'directory') {
      expandedPaths.value.add(path)
      selectedPath.value = path
      viewingFile.value = null
      isLoadingContent.value = false
      await ensureDirectoryLoaded(workspaceId, path)
      return
    }

    selectFile(path, workspaceId)
  }

  function toggleExpand(path: string, workspaceId: string): void {
    if (expandedPaths.value.has(path)) {
      expandedPaths.value.delete(path)
    } else {
      expandedPaths.value.add(path)
      // Fetch children if not loaded
      fetchDirectory(workspaceId, path)
    }
  }

  function selectFile(path: string, workspaceId: string): void {
    selectedPath.value = path
    fetchFileContent(workspaceId, path)
  }

  const IMAGE_EXTENSIONS: Record<string, string> = {
    png: 'image/png',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    gif: 'image/gif',
    webp: 'image/webp',
    svg: 'image/svg+xml',
    bmp: 'image/bmp',
    ico: 'image/x-icon',
    tiff: 'image/tiff',
    tif: 'image/tiff',
    avif: 'image/avif',
  }

  function getFileExtension(path: string): string {
    const name = path.split('/').pop() ?? ''
    const dotIndex = name.lastIndexOf('.')
    return dotIndex >= 0 ? name.slice(dotIndex + 1).toLowerCase() : ''
  }

  function setFileContent(
    path: string,
    content: string,
    size: number,
    truncated: boolean,
    mimeType?: string,
  ): void {
    const ext = getFileExtension(path)
    const imageMime = IMAGE_EXTENSIONS[ext]
    const clean = (content ?? '').replace(/\s+/g, '')
    // Decoded bytes actually received (base64 estimate, no padding error).
    const previewBytes = Math.floor((clean.length * 3) / 4)

    // Determine media type from file extension first
    if (imageMime) {
      viewingFile.value = {
        path,
        content: '',
        rawBase64: content,
        size,
        truncated: false,
        previewBytes,
        isBinary: true,
        mediaType: 'image',
        mimeType: imageMime,
      }
      isLoadingContent.value = false
      return
    }

    if (ext === 'pdf') {
      viewingFile.value = {
        path,
        content: '',
        rawBase64: content,
        size,
        truncated: false,
        previewBytes,
        isBinary: true,
        mediaType: 'pdf',
        mimeType: 'application/pdf',
      }
      isLoadingContent.value = false
      return
    }

    // Try to detect binary content and properly decode UTF-8
    let decoded: string
    let isBinary = false
    try {
      const bytes = Uint8Array.from(atob(content), (c) => c.charCodeAt(0))
      // Check for null bytes — a simple binary heuristic
      if (bytes.includes(0)) {
        isBinary = true
        decoded = ''
      } else {
        decoded = new TextDecoder('utf-8').decode(bytes)
      }
    } catch {
      isBinary = true
      decoded = ''
    }

    viewingFile.value = {
      path,
      content: decoded,
      rawBase64: content,
      size,
      truncated,
      previewBytes,
      isBinary,
      mediaType: isBinary ? 'binary' : 'text',
      mimeType: mimeType ?? 'text/plain',
    }
    isLoadingContent.value = false
    contentError.value = null
  }

  function cancelContentTransfer(): void {
    contentController?.abort()
    contentController = null
  }

  function closeFileViewer(): void {
    cancelContentTransfer()
    for (const controller of downloadControllers) controller.abort()
    downloadControllers.clear()
    viewingFile.value = null
    selectedPath.value = null
    isLoadingContent.value = false
    contentError.value = null
  }

  function reset(): void {
    closeFileViewer()
    tree.value = []
    expandedPaths.value = new Set()
    loadingPaths.value = new Set()
    pendingRequests.value.clear()
    pendingUploadRefreshPolicy.clear()
    pendingUploadRequests.clear()
    for (const timer of terminalUploadRequestIds.values()) clearTimeout(timer)
    terminalUploadRequestIds.clear()
    for (const request of pendingDirectoryRequests.values()) {
      request.signal?.removeEventListener('abort', request.abort!)
      request.onResult(null)
    }
    pendingDirectoryRequests.clear()
    pendingFindRequests.value.clear()
    for (const timer of pendingTimers.values()) clearTimeout(timer)
    pendingTimers.clear()
  }

  // -- socket request helpers -----------------------------------------------

  function requestDirectory(
    workspaceId: string,
    path: string,
    updateTree: boolean,
    signal?: AbortSignal,
  ): Promise<FileEntryRaw[] | null> {
    if (useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return Promise.resolve(null)
    if (updateTree) loadingPaths.value.add(path)
    const requestId = nextRequestId()
    return new Promise((resolve) => {
      const request: {
        workspaceId: string
        path: string
        updateTree: boolean
        onResult: (entries: FileEntryRaw[] | null) => void
        signal?: AbortSignal
        abort?: () => void
      } = {
        workspaceId,
        path,
        updateTree,
        onResult: resolve,
        signal,
      }
      const abort = (): void => {
        if (pendingDirectoryRequests.get(requestId) !== request) return
        pendingDirectoryRequests.delete(requestId)
        signal?.removeEventListener('abort', abort)
        clearPendingTimer(requestId)
        if (updateTree) loadingPaths.value.delete(path)
        resolve(null)
      }
      request.abort = abort
      if (signal?.aborted) {
        resolve(null)
        return
      }
      signal?.addEventListener('abort', abort, { once: true })
      pendingDirectoryRequests.set(requestId, request)
      armPendingTimer(requestId, () => {
        pendingDirectoryRequests.delete(requestId)
        signal?.removeEventListener('abort', abort)
        if (updateTree) loadingPaths.value.delete(path)
        resolve(null)
      })
      try {
        sendFilesList(workspaceId, requestId, path)
      } catch {
        clearPendingTimer(requestId)
        pendingDirectoryRequests.delete(requestId)
        signal?.removeEventListener('abort', abort)
        if (updateTree) loadingPaths.value.delete(path)
        resolve(null)
      }
    })
  }

  function fetchDirectory(workspaceId: string, path: string): Promise<void> {
    return requestDirectory(workspaceId, path, true).then(() => undefined)
  }

  /** Fetch a directory listing without changing the shared explorer tree. */
  function fetchDirectoryEntries(
    workspaceId: string,
    path: string,
    options: { signal?: AbortSignal } = {},
  ): Promise<FileEntryRaw[] | null> {
    return requestDirectory(workspaceId, path, false, options.signal)
  }

  function findFiles(workspaceId: string, query: string, limit = 50): Promise<string[]> {
    const requestId = nextRequestId()
    return new Promise((resolve) => {
      pendingFindRequests.value.set(requestId, (paths) => {
        resolve(paths)
      })
      armPendingTimer(requestId, () => {
        pendingFindRequests.value.delete(requestId)
        resolve([])
      })
      if (!sendFilesFind(workspaceId, requestId, query, limit)) {
        clearPendingTimer(requestId)
        pendingFindRequests.value.delete(requestId)
        resolve([])
      }
    })
  }

  async function fetchFileContent(workspaceId: string, path: string): Promise<void> {
    if (useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
    cancelContentTransfer()
    const controller = new AbortController()
    contentController = controller
    isLoadingContent.value = true
    contentError.value = null
    try {
      const result = await readWorkspaceFile(workspaceId, path, undefined, controller.signal)
      if (controller.signal.aborted) return
      setFileContent(path, result.content, result.size, result.truncated, result.mime_type)
    } catch (error) {
      if (controller.signal.aborted) return
      const message = error instanceof Error ? error.message : 'File read failed. Please retry.'
      contentError.value = message
      useNotificationStore().error('File read failed', message)
    } finally {
      if (contentController === controller) {
        contentController = null
        isLoadingContent.value = false
      }
    }
  }

  async function downloadFile(workspaceId: string, path: string): Promise<void> {
    if (useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
    const controller = new AbortController()
    downloadControllers.add(controller)
    try {
      const result = await downloadWorkspaceFile(workspaceId, path, controller.signal)
      if (!controller.signal.aborted) {
        triggerBrowserDownload(result.content, result.filename, result.is_archive)
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        useNotificationStore().error(
          'Download failed',
          error instanceof Error ? error.message : 'Download failed. Please retry.',
        )
      }
    } finally {
      downloadControllers.delete(controller)
    }
  }

  function refreshDirectory(workspaceId: string, path: string): void {
    fetchDirectory(workspaceId, path)
  }

  function refreshAll(workspaceId: string): void {
    fetchDirectory(workspaceId, '/workspace')
    for (const path of expandedPaths.value) {
      fetchDirectory(workspaceId, path)
    }
  }

  // -- event handlers (called from WorkspaceDetailView) ---------------------

  function shouldRefreshUploadResult(requestId: string): boolean {
    return pendingUploadRefreshPolicy.get(requestId) ?? true
  }

  function isTerminalUpload(requestId: string): boolean {
    return terminalUploadRequestIds.has(requestId)
  }

  function markUploadTerminal(requestId: string): void {
    const previousTimer = terminalUploadRequestIds.get(requestId)
    if (previousTimer) clearTimeout(previousTimer)
    const timer = setTimeout(
      () => terminalUploadRequestIds.delete(requestId),
      UPLOAD_TERMINAL_TTL_MS,
    )
    terminalUploadRequestIds.delete(requestId)
    terminalUploadRequestIds.set(requestId, timer)

    // Keep the cache bounded even if many uploads complete inside the TTL.
    while (terminalUploadRequestIds.size > MAX_TERMINAL_UPLOAD_IDS) {
      const oldestId = terminalUploadRequestIds.keys().next().value
      if (oldestId === undefined) break
      const oldestTimer = terminalUploadRequestIds.get(oldestId)
      if (oldestTimer) clearTimeout(oldestTimer)
      terminalUploadRequestIds.delete(oldestId)
    }
  }

  function handleListResult(
    requestId: string,
    path: string,
    entries: FileEntryRaw[],
    error?: string,
    workspaceId?: string,
    onlyKnownRequests = false,
  ): void {
    const directoryRequest = pendingDirectoryRequests.get(requestId)
    if (directoryRequest) {
      if (
        directoryRequest.path !== path ||
        (workspaceId !== undefined && directoryRequest.workspaceId !== workspaceId)
      )
        return
      pendingDirectoryRequests.delete(requestId)
      directoryRequest.signal?.removeEventListener('abort', directoryRequest.abort!)
      clearPendingTimer(requestId)
      if (directoryRequest.updateTree) loadingPaths.value.delete(path)
      const result = error ? null : entries
      if (directoryRequest.updateTree && result) setTree(path, result)
      directoryRequest.onResult(result)
      return
    }
    if (onlyKnownRequests) return

    const callback = pendingRequests.value.get(requestId)
    if (callback) {
      callback(null)
      pendingRequests.value.delete(requestId)
    }
    clearPendingTimer(requestId)
    loadingPaths.value.delete(path)

    if (error) {
      console.warn('[fileExplorer] list error:', error)
      return
    }

    setTree(path, entries)
  }

  function handleFindResult(requestId: string, paths: string[], error?: string): void {
    const callback = pendingFindRequests.value.get(requestId)
    if (!callback) return
    pendingFindRequests.value.delete(requestId)
    clearPendingTimer(requestId)
    callback(error ? [] : paths)
  }

  function handleUploadResult(
    requestId: string,
    path: string,
    status: string,
    workspaceId: string,
    error?: string,
    refreshExplorerOverride?: boolean,
  ): boolean {
    // Results are broadcast to each mounted workspace listener. Retain a
    // terminal id until expiry instead of consuming it on the first duplicate.
    if (isTerminalUpload(requestId)) return false

    const request = pendingUploadRequests.get(requestId)
    if (!request) {
      // Upload results belong to this client only when we registered them.
      return false
    }
    // Fail closed if another workspace or path accidentally reuses an id.
    if (request.workspaceId !== workspaceId || request.path !== path) return false

    const callback = pendingRequests.value.get(requestId)
    const refreshExplorer =
      refreshExplorerOverride ?? pendingUploadRefreshPolicy.get(requestId) ?? true
    pendingUploadRequests.delete(requestId)
    pendingUploadRefreshPolicy.delete(requestId)
    markUploadTerminal(requestId)
    if (callback) {
      pendingRequests.value.delete(requestId)
      clearPendingTimer(requestId)
      callback(
        error || status === 'error'
          ? { ok: false, error: error ?? 'The file could not be uploaded.' }
          : { ok: true },
      )
    } else {
      clearPendingTimer(requestId)
    }
    if (error || status === 'error') {
      const notify = useNotificationStore()
      notify.error('Upload failed', error ?? 'The file could not be uploaded.')
      return true
    }

    // Refresh unless an isolated scheduled-task upload owns this result.
    if (refreshExplorer) refreshDirectory(workspaceId, path)
    return true
  }

  /**
   * Register a pending upload and return a Promise that resolves when the
   * `files:upload_result` succeeds for the given requestId.
   * Rejects on error and times out after 30 s so uploads never hang.
   * The pending callback receives `{ ok: true }` on success or
   * `{ ok: false, error }` on failure so resolve/reject stay explicit.
   */
  function trackAndUpload(
    _workspaceId: string,
    requestId: string,
    _path: string,
    _filename: string,
    _content: string,
    refreshExplorer = true,
  ): Promise<void> {
    if (isTerminalUpload(requestId))
      return Promise.reject(new Error('Upload request already finished.'))
    pendingUploadRefreshPolicy.set(requestId, refreshExplorer)
    pendingUploadRequests.set(requestId, { workspaceId: _workspaceId, path: _path })
    return new Promise<void>((resolve, reject) => {
      pendingRequests.value.set(requestId, (data: unknown) => {
        pendingUploadRefreshPolicy.delete(requestId)
        pendingUploadRequests.delete(requestId)
        clearPendingTimer(requestId)
        const ok =
          data !== null && typeof data === 'object' && (data as { ok?: boolean }).ok === true
        if (ok) {
          resolve()
        } else {
          const message =
            data !== null &&
            typeof data === 'object' &&
            typeof (data as { error?: unknown }).error === 'string'
              ? ((data as { error: string }).error ?? 'The file could not be uploaded.')
              : 'The file could not be uploaded.'
          reject(new Error(message))
        }
      })
      armPendingTimer(requestId, () => {
        pendingRequests.value.delete(requestId)
        pendingUploadRefreshPolicy.delete(requestId)
        pendingUploadRequests.delete(requestId)
        markUploadTerminal(requestId)
        const message = 'Upload timed out. Please retry.'
        useNotificationStore().error('Upload failed', message)
        reject(new Error(message))
      })
    })
  }

  /**
   * Cancel a tracked upload without a notification (e.g. its owning composer
   * was reset or unmounted). Late results are ignored so cancellation stays
   * silent and cannot refresh the shared explorer tree.
   */
  function cancelUpload(requestId: string): void {
    const callback = pendingRequests.value.get(requestId)
    if (!callback) return
    pendingRequests.value.delete(requestId)
    pendingUploadRefreshPolicy.delete(requestId)
    pendingUploadRequests.delete(requestId)
    clearPendingTimer(requestId)
    markUploadTerminal(requestId)
    callback({ ok: false, error: 'Upload cancelled.' })
  }

  /**
   * Fail a tracked upload immediately (e.g. synchronous send error).
   * Runs the pending callback with `{ ok: false }` so the trackAndUpload
   * promise rejects at once instead of hanging until the 30 s timeout,
   * and surfaces a visible error. No-op for unknown request ids.
   */
  function failUpload(requestId: string, message: string): void {
    const callback = pendingRequests.value.get(requestId)
    if (!callback || !pendingUploadRequests.has(requestId) || isTerminalUpload(requestId)) return
    clearPendingTimer(requestId)
    pendingUploadRefreshPolicy.delete(requestId)
    pendingUploadRequests.delete(requestId)
    pendingRequests.value.delete(requestId)
    markUploadTerminal(requestId)
    callback({ ok: false, error: message })
    useNotificationStore().error('Upload failed', message)
  }

  /** Save the validated shared-client payload and release its browser URL. */
  function triggerBrowserDownload(content: string, filename: string, isArchive: boolean): void {
    const raw = atob(content.replace(/\s+/g, ''))
    const bytes = Uint8Array.from(raw, (character) => character.charCodeAt(0))
    const mimeType = isArchive ? 'application/gzip' : 'application/octet-stream'
    const blob = new Blob([bytes.buffer], { type: mimeType })
    const url = URL.createObjectURL(blob)
    let anchor: HTMLAnchorElement | null = null
    try {
      anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
    } finally {
      anchor?.remove()
      setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_REVOKE_DELAY_MS)
    }
  }

  /** Retry the currently viewed file after a visible read error. */
  function retryViewingFile(workspaceId: string): void {
    const path = selectedPath.value
    if (!path) return
    fetchFileContent(workspaceId, path)
  }

  return {
    // state
    tree,
    expandedPaths,
    selectedPath,
    viewingFile,
    isLoadingContent,
    contentError,
    loadingPaths,
    // getters
    isViewingFile,
    // actions
    setTree,
    toggleExpand,
    selectFile,
    openPath,
    setFileContent,
    closeFileViewer,
    reset,
    fetchDirectory,
    fetchDirectoryEntries,
    findFiles,
    downloadFile,
    refreshAll,
    trackAndUpload,
    cancelUpload,
    failUpload,
    retryViewingFile,
    formatBytes,
    // event handlers
    handleListResult,
    shouldRefreshUploadResult,
    handleFindResult,
    handleUploadResult,
  }
})
