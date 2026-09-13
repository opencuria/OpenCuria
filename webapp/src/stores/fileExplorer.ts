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
  sendFilesRead,
  sendFilesDownload,
} from '@/services/socket'
import {
  createChunkedTransferStore,
  decodedBase64Size,
  stripBase64Whitespace,
  type ChunkedTransferStore,
} from '@/lib/fileChunks'
import { useNotificationStore } from '@/stores/notifications'
import { useSidePanelStore } from '@/stores/sidePanel'

let requestCounter = 0

function nextRequestId(): string {
  return `files-${++requestCounter}-${Date.now()}`
}

/** Max buffered base64 chars for one chunked read/download (~140 MiB chars). */
const CONTENT_CHUNK_MAX_CHARS = 140 * 1024 * 1024

/** Max raw bytes accepted for one browser download (matches runner cap). */
const DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024

/** Delay before a download object URL is revoked (lets the click dispatch). */
const DOWNLOAD_REVOKE_DELAY_MS = 1000

/** How long a file read may stay pending before failing visibly. */
const CONTENT_TIMEOUT_MS = 30_000

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
  const activeContentRequestId = ref<string | null>(null)

  // Pending request callbacks: request_id → resolver
  const pendingRequests = ref<Map<string, (data: unknown) => void>>(new Map())
  const pendingFindRequests = ref<Map<string, (paths: string[]) => void>>(new Map())

  // Bounded reassembly for chunked content/download transfers.
  let contentChunks: ChunkedTransferStore | null = null
  let downloadChunks: ChunkedTransferStore | null = null
  // request_id → expected path for in-flight content/download reads.
  // The first chunk pins the transfer with this path; a chunk for any
  // other path fails closed instead of becoming ground truth.
  const pendingContentPaths = new Map<string, string>()
  const pendingDownloadPaths = new Map<string, string>()
  // request_id → timeout handle for pending reads/downloads/uploads.
  const pendingTimers = new Map<string, ReturnType<typeof setTimeout>>()

  function getContentChunks(): ChunkedTransferStore {
    if (!contentChunks) {
      contentChunks = createChunkedTransferStore({
        maxChars: CONTENT_CHUNK_MAX_CHARS,
        onTimeout: (requestId) => failContentTransfer(requestId, 'File read timed out. Please retry.'),
      })
    }
    return contentChunks
  }

  function getDownloadChunks(): ChunkedTransferStore {
    if (!downloadChunks) {
      downloadChunks = createChunkedTransferStore({
        maxChars: CONTENT_CHUNK_MAX_CHARS,
        onTimeout: (requestId) => failDownloadTransfer(requestId, 'Download timed out. Please retry.'),
      })
    }
    return downloadChunks
  }

  function clearPendingTimer(requestId: string): void {
    const timer = pendingTimers.get(requestId)
    if (timer) {
      clearTimeout(timer)
      pendingTimers.delete(requestId)
    }
  }

  function armPendingTimer(requestId: string, onFire: () => void, ms = CONTENT_TIMEOUT_MS): void {
    clearPendingTimer(requestId)
    const timer = setTimeout(() => {
      pendingTimers.delete(requestId)
      onFire()
    }, ms)
    pendingTimers.set(requestId, timer)
  }

  function isStaleContentResult(requestId: string): boolean {
    return activeContentRequestId.value !== null && activeContentRequestId.value !== requestId
  }

  function failContentTransfer(requestId: string, message: string): void {
    getContentChunks().cancel(requestId)
    clearPendingTimer(requestId)
    pendingRequests.value.delete(requestId)
    pendingContentPaths.delete(requestId)
    // Ignore late failures for transfers the user already moved past.
    if (isStaleContentResult(requestId)) return
    if (activeContentRequestId.value === requestId) {
      activeContentRequestId.value = null
    }
    isLoadingContent.value = false
    contentError.value = message
    useNotificationStore().error('File read failed', message)
  }

  function failDownloadTransfer(requestId: string, message: string): void {
    getDownloadChunks().cancel(requestId)
    clearPendingTimer(requestId)
    pendingRequests.value.delete(requestId)
    pendingDownloadPaths.delete(requestId)
    useNotificationStore().error('Download failed', message)
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

  function setChildNodes(
    nodes: FileNode[],
    parentPath: string,
    children: FileNode[],
  ): void {
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

  async function ensureDirectoryLoaded(
    workspaceId: string,
    path: string,
  ): Promise<void> {
    const node = findNode(tree.value, path)
    if (path !== '/workspace' && (!node || node.type !== 'directory')) {
      return
    }
    if (path !== '/workspace' && node?.children) {
      return
    }
    await fetchDirectory(workspaceId, path)
  }

  async function openPath(
    workspaceId: string,
    rawPath: string,
    options: { revealInExplorer?: boolean } = {},
  ): Promise<void> {
    const { revealInExplorer = true } = options
    const path = decodeURIComponent(rawPath.trim()).replace(/[?#].*$/, '')
    if (!path.startsWith('/workspace')) {
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

  function toggleExpand(
    path: string,
    workspaceId: string,
  ): void {
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

  /** Actively cancel an in-flight content read (new click or close). */
  function cancelContentTransfer(requestId: string | null): void {
    if (!requestId) return
    getContentChunks().cancel(requestId)
    clearPendingTimer(requestId)
    pendingRequests.value.delete(requestId)
    pendingContentPaths.delete(requestId)
    if (activeContentRequestId.value === requestId) {
      activeContentRequestId.value = null
    }
  }

  function cancelDownloadTransfer(requestId: string | null): void {
    if (!requestId) return
    getDownloadChunks().cancel(requestId)
    clearPendingTimer(requestId)
    pendingRequests.value.delete(requestId)
    pendingDownloadPaths.delete(requestId)
  }

  function closeFileViewer(): void {
    cancelContentTransfer(activeContentRequestId.value)
    getDownloadChunks().clear()
    for (const [requestId] of pendingDownloadPaths) {
      clearPendingTimer(requestId)
      pendingRequests.value.delete(requestId)
    }
    pendingDownloadPaths.clear()
    viewingFile.value = null
    selectedPath.value = null
    isLoadingContent.value = false
    contentError.value = null
  }

  function reset(): void {
    tree.value = []
    expandedPaths.value = new Set()
    selectedPath.value = null
    viewingFile.value = null
    isLoadingContent.value = false
    contentError.value = null
    activeContentRequestId.value = null
    loadingPaths.value = new Set()
    pendingRequests.value.clear()
    pendingFindRequests.value.clear()
    pendingContentPaths.clear()
    pendingDownloadPaths.clear()
    contentChunks?.clear()
    downloadChunks?.clear()
    for (const timer of pendingTimers.values()) clearTimeout(timer)
    pendingTimers.clear()
  }

  // -- socket request helpers -----------------------------------------------

  function fetchDirectory(workspaceId: string, path: string): Promise<void> {
    loadingPaths.value.add(path)
    const requestId = nextRequestId()
    return new Promise<void>((resolve) => {
      pendingRequests.value.set(requestId, () => {
        loadingPaths.value.delete(path)
        resolve()
      })
      armPendingTimer(requestId, () => {
        pendingRequests.value.delete(requestId)
        loadingPaths.value.delete(path)
        resolve()
      })
      sendFilesList(workspaceId, requestId, path)
    })
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

  function fetchFileContent(workspaceId: string, path: string): void {
    // A newer click actively cancels the older in-flight read so its late
    // chunks/results can never populate the viewer.
    cancelContentTransfer(activeContentRequestId.value)
    isLoadingContent.value = true
    contentError.value = null
    const requestId = nextRequestId()
    activeContentRequestId.value = requestId
    pendingRequests.value.set(requestId, () => {
      // cleanup handled by setFileContent
    })
    pendingContentPaths.set(requestId, path)
    armPendingTimer(requestId, () =>
      failContentTransfer(requestId, 'File read timed out. Please retry.'),
    )
    sendFilesRead(workspaceId, requestId, path)
  }

  function downloadFile(workspaceId: string, path: string): void {
    const requestId = nextRequestId()
    pendingRequests.value.set(requestId, () => {})
    pendingDownloadPaths.set(requestId, path)
    armPendingTimer(requestId, () =>
      failDownloadTransfer(requestId, 'Download timed out. Please retry.'),
    )
    sendFilesDownload(workspaceId, requestId, path)
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

  function handleListResult(
    requestId: string,
    path: string,
    entries: FileEntryRaw[],
    error?: string,
  ): void {
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

  function handleContentResult(
    requestId: string,
    path: string,
    content: string,
    size: number,
    truncated: boolean,
    error?: string,
    options: { chunked?: boolean; totalChunks?: number; mimeType?: string } = {},
  ): void {
    // Ignore results we didn't request (e.g. from workspaceImages store)
    if (!pendingRequests.value.has(requestId) && !getContentChunks().has(requestId)) return
    // Stale results from a superseded click are dropped silently.
    if (isStaleContentResult(requestId)) {
      getContentChunks().cancel(requestId)
      clearPendingTimer(requestId)
      pendingRequests.value.delete(requestId)
      pendingContentPaths.delete(requestId)
      return
    }

    if (error) {
      failContentTransfer(requestId, error)
      return
    }

    // The final result path must match the requested path; a mismatch
    // fails closed so a cross-file reply can never populate the viewer.
    const expectedPath = pendingContentPaths.get(requestId) ?? getContentChunks().getPath(requestId)
    if (expectedPath && path !== expectedPath) {
      failContentTransfer(requestId, `Unexpected file path for ${requestId}. Please retry.`)
      return
    }

    // Chunked transfer: the final result carries metadata only; join the
    // buffered slices first. Chunks carry payload only — authoritative
    // size/mime/truncated come from this final result.
    if (options.chunked) {
      try {
        const assembled = getContentChunks().finish(requestId, options.totalChunks)
        // The final total must exactly match the buffered transfer.
        if (
          options.totalChunks !== undefined &&
          options.totalChunks !== assembled.meta.totalChunks &&
          assembled.meta.totalChunks !== undefined
        ) {
          throw new Error(`total_chunks mismatch for ${requestId}`)
        }
        if (assembled.path && assembled.path !== expectedPath && expectedPath) {
          throw new Error(`path mismatch for ${requestId}`)
        }
        const callback = pendingRequests.value.get(requestId)
        if (callback) {
          callback(null)
          pendingRequests.value.delete(requestId)
        }
        pendingContentPaths.delete(requestId)
        clearPendingTimer(requestId)
        activeContentRequestId.value = null
        setFileContent(
          expectedPath ?? assembled.path ?? path,
          assembled.content,
          size,
          truncated,
          options.mimeType,
        )
      } catch (err) {
        failContentTransfer(
          requestId,
          err instanceof Error ? err.message : 'Incomplete file transfer. Please retry.',
        )
      }
      return
    }

    const callback = pendingRequests.value.get(requestId)
    if (callback) {
      callback(null)
      pendingRequests.value.delete(requestId)
    }
    pendingContentPaths.delete(requestId)
    clearPendingTimer(requestId)
    activeContentRequestId.value = null

    setFileContent(path, content, size, truncated, options.mimeType)
  }

  function handleContentChunk(
    requestId: string,
    path: string,
    index: number,
    totalChunks: number,
    content: string,
  ): void {
    // Chunks carry payload only (no size/mime/truncated metadata); the
    // final result carries authoritative metadata. Ignore chunks we
    // didn't request (e.g. from workspaceImages store).
    if (!pendingRequests.value.has(requestId) && !getContentChunks().has(requestId)) return
    // Path/request validation: a chunk for the wrong path fails closed.
    if (isStaleContentResult(requestId)) {
      getContentChunks().cancel(requestId)
      clearPendingTimer(requestId)
      pendingRequests.value.delete(requestId)
      pendingContentPaths.delete(requestId)
      return
    }
    try {
      const store = getContentChunks()
      if (!store.has(requestId)) {
        if (!Number.isInteger(totalChunks) || totalChunks <= 0) {
          throw new Error(`invalid total_chunks: ${String(totalChunks)}`)
        }
        // Pin the transfer with the requested path, not the chunk's own
        // claim, so the first wrong-path chunk fails instead of becoming
        // ground truth.
        store.start(requestId, totalChunks, { totalChunks }, pendingContentPaths.get(requestId) ?? path)
      }
      store.addChunk(requestId, {
        workspace_id: '',
        request_id: requestId,
        path,
        index,
        total_chunks: totalChunks,
        content,
      })
    } catch (err) {
      failContentTransfer(
        requestId,
        err instanceof Error ? err.message : 'Invalid file transfer. Please retry.',
      )
    }
  }

  function handleUploadResult(
    requestId: string,
    path: string,
    status: string,
    workspaceId: string,
    error?: string,
  ): void {
    const callback = pendingRequests.value.get(requestId)
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
      return
    }

    // Refresh the parent directory
    refreshDirectory(workspaceId, path)
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
  ): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      pendingRequests.value.set(requestId, (data: unknown) => {
        clearPendingTimer(requestId)
        const ok =
          data !== null &&
          typeof data === 'object' &&
          (data as { ok?: boolean }).ok === true
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
      armPendingTimer(
        requestId,
        () => {
          pendingRequests.value.delete(requestId)
          const message = 'Upload timed out. Please retry.'
          useNotificationStore().error('Upload failed', message)
          reject(new Error(message))
        },
      )
    })
  }

  /**
   * Fail a tracked upload immediately (e.g. synchronous send error).
   * Runs the pending callback with `{ ok: false }` so the trackAndUpload
   * promise rejects at once instead of hanging until the 30 s timeout,
   * and surfaces a visible error. No-op for unknown request ids.
   */
  function failUpload(requestId: string, message: string): void {
    const callback = pendingRequests.value.get(requestId)
    clearPendingTimer(requestId)
    if (callback) {
      pendingRequests.value.delete(requestId)
      callback({ ok: false, error: message })
    }
    useNotificationStore().error('Upload failed', message)
  }

  function triggerBrowserDownload(
    content: string,
    filename: string,
    isArchive: boolean,
    expectedSize: number | null = null,
  ): void {
    triggerValidatedDownload(content, filename, isArchive, expectedSize)
  }

  /**
   * Validate + decode base64, then trigger the anchor download.
   * Exact raw-size guard (100 MiB) runs on the padding-aware decoded
   * size before `atob`; when `expectedSize` is known the decoded bytes
   * must match it exactly (the runner's `size` is the archive size for
   * directory downloads). Any failure surfaces `Download failed` and
   * cleans up pending state — nothing escapes uncaught. The object URL
   * is revoked after a short delay so the click can dispatch first.
   */
  function triggerValidatedDownload(
    content: string,
    filename: string,
    isArchive: boolean,
    expectedSize: number | null,
  ): void {
    // Visible failure first: any exception below must end here so the
    // caller never leaves an uncaught error or a dangling pending entry.
    const fail = (message: string): void => {
      useNotificationStore().error('Download failed', message)
    }
    try {
      const clean = stripBase64Whitespace(content ?? '')
      // Exact decoded size (padding-aware) — rejects malformed base64
      // before atob/Blob/URL allocation.
      const decodedSize = decodedBase64Size(clean)
      if (decodedSize > DOWNLOAD_MAX_BYTES) {
        throw new Error(
          `Download exceeds the ${formatBytes(DOWNLOAD_MAX_BYTES)} limit.`,
        )
      }
      if (expectedSize !== null) {
        if (!Number.isFinite(expectedSize) || !Number.isInteger(expectedSize) || expectedSize < 0 || expectedSize > DOWNLOAD_MAX_BYTES) {
          throw new Error('Invalid download size reported by the runner.')
        }
        // Runner `size` is raw bytes (archive size for directories), so
        // it must equal the exact decoded payload size.
        if (decodedSize !== expectedSize) {
          throw new Error('Download size mismatch. Please retry.')
        }
      }
      const raw = atob(clean)
      const bytes = new Uint8Array(raw.length)
      for (let i = 0; i < raw.length; i++) {
        bytes[i] = raw.charCodeAt(i)
      }
      const mimeType = isArchive ? 'application/gzip' : 'application/octet-stream'
      const blob = new Blob([bytes.buffer as ArrayBuffer], { type: mimeType })
      const url = URL.createObjectURL(blob)
      let anchor: HTMLAnchorElement | null = null
      try {
        anchor = document.createElement('a')
        anchor.href = url
        anchor.download = filename
        // Attach briefly so every browser dispatches the click reliably.
        document.body.appendChild(anchor)
        anchor.click()
      } finally {
        if (anchor?.parentNode) anchor.remove()
        setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_REVOKE_DELAY_MS)
      }
    } catch (err) {
      fail(err instanceof Error ? err.message : 'Download failed. Please retry.')
    }
  }

  function handleDownloadResult(
    requestId: string,
    content: string,
    filename: string,
    isArchive: boolean,
    error?: string,
    options: { chunked?: boolean; totalChunks?: number; size?: number } = {},
  ): void {
    if (!pendingRequests.value.has(requestId) && !getDownloadChunks().has(requestId)) return

    if (error) {
      failDownloadTransfer(requestId, error)
      return
    }

    if (options.chunked) {
      let assembledContent: string | null = null
      try {
        const assembled = getDownloadChunks().finish(requestId, options.totalChunks)
        const expectedPath = pendingDownloadPaths.get(requestId) ?? assembled.path
        if (assembled.path && expectedPath && assembled.path !== expectedPath) {
          throw new Error(`path mismatch for ${requestId}`)
        }
        // Validate the reported archive/file size before touching atob:
        // finite, in range, and exactly equal to the decoded payload.
        validateDownloadSize(options.size, assembled.content)
        const callback = pendingRequests.value.get(requestId)
        if (callback) {
          callback(null)
          pendingRequests.value.delete(requestId)
        }
        pendingDownloadPaths.delete(requestId)
        clearPendingTimer(requestId)
        assembledContent = assembled.content
      } catch (err) {
        failDownloadTransfer(
          requestId,
          err instanceof Error ? err.message : 'Incomplete download. Please retry.',
        )
        return
      }
      // Outside try/catch bookkeeping: triggerValidatedDownload reports
      // its own visible `Download failed` and never throws.
      triggerBrowserDownload(assembledContent, filename, isArchive, options.size ?? null)
      return
    }

    const callback = pendingRequests.value.get(requestId)
    if (callback) {
      callback(null)
      pendingRequests.value.delete(requestId)
    }
    pendingDownloadPaths.delete(requestId)
    clearPendingTimer(requestId)

    triggerValidatedDownload(content, filename, isArchive, options.size ?? null)
  }

  /**
   * Validate the runner-reported `size` against the decoded payload.
   * Throws on non-finite/negative/>100MiB or on decoded-size mismatch.
   * Runs before `atob` so oversize/invalid payloads never allocate.
   */
  function validateDownloadSize(size: number | undefined, content: string): void {
    if (size === undefined) return
    if (!Number.isFinite(size) || !Number.isInteger(size) || size < 0 || size > DOWNLOAD_MAX_BYTES) {
      throw new Error('Invalid download size reported by the runner.')
    }
    const decodedSize = decodedBase64Size(stripBase64Whitespace(content ?? ''))
    if (decodedSize > DOWNLOAD_MAX_BYTES) {
      throw new Error(`Download exceeds the ${formatBytes(DOWNLOAD_MAX_BYTES)} limit.`)
    }
    if (decodedSize !== size) {
      throw new Error('Download size mismatch. Please retry.')
    }
  }

  function handleDownloadChunk(
    requestId: string,
    path: string,
    index: number,
    totalChunks: number,
    content: string,
  ): void {
    // Download chunks carry payload only; filename/archive/size come from
    // the final result.
    if (!pendingRequests.value.has(requestId) && !getDownloadChunks().has(requestId)) return
    try {
      const store = getDownloadChunks()
      if (!store.has(requestId)) {
        // Pin with the requested path so the first wrong-path chunk fails.
        store.start(requestId, totalChunks, { totalChunks }, pendingDownloadPaths.get(requestId) ?? path)
      }
      store.addChunk(requestId, {
        workspace_id: '',
        request_id: requestId,
        path,
        index,
        total_chunks: totalChunks,
        content,
      })
    } catch (err) {
      failDownloadTransfer(
        requestId,
        err instanceof Error ? err.message : 'Invalid download. Please retry.',
      )
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
    findFiles,
    downloadFile,
    refreshAll,
    trackAndUpload,
    failUpload,
    retryViewingFile,
    formatBytes,
    // event handlers
    handleListResult,
    handleFindResult,
    handleContentResult,
    handleContentChunk,
    handleUploadResult,
    handleDownloadResult,
    handleDownloadChunk,
  }
})
