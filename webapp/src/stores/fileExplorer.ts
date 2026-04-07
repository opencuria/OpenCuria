/**
 * File explorer store — manages the file explorer panel state,
 * tree structure, and file viewing.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { FileNode, FileEntryRaw } from '@/types'
import {
  sendFilesList,
  sendFilesRead,
  sendFilesDownload,
} from '@/services/socket'
import { useNotificationStore } from '@/stores/notifications'

let requestCounter = 0

function nextRequestId(): string {
  return `files-${++requestCounter}-${Date.now()}`
}

export const useFileExplorerStore = defineStore('fileExplorer', () => {
  // -- state ----------------------------------------------------------------

  const isOpen = ref(false)
  const panelWidth = ref(300)
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
    isBinary: boolean
    mediaType: 'text' | 'image' | 'pdf' | 'binary'
    mimeType: string
  } | null>(null)
  const isLoadingContent = ref(false)

  // Pending request callbacks: request_id → resolver
  const pendingRequests = ref<Map<string, (data: unknown) => void>>(new Map())

  // -- getters --------------------------------------------------------------

  const isViewingFile = computed(() => viewingFile.value !== null)

  // -- actions --------------------------------------------------------------

  function open(): void {
    isOpen.value = true
  }

  function close(): void {
    isOpen.value = false
  }

  function toggle(): void {
    isOpen.value = !isOpen.value
  }

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
      open()
    } else {
      close()
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
  ): void {
    const ext = getFileExtension(path)
    const imageMime = IMAGE_EXTENSIONS[ext]

    // Determine media type from file extension first
    if (imageMime) {
      viewingFile.value = {
        path,
        content: '',
        rawBase64: content,
        size,
        truncated: false,
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
      isBinary,
      mediaType: isBinary ? 'binary' : 'text',
      mimeType: 'text/plain',
    }
    isLoadingContent.value = false
  }

  function closeFileViewer(): void {
    viewingFile.value = null
    selectedPath.value = null
    isLoadingContent.value = false
  }

  function reset(): void {
    isOpen.value = false
    tree.value = []
    expandedPaths.value = new Set()
    selectedPath.value = null
    viewingFile.value = null
    isLoadingContent.value = false
    loadingPaths.value = new Set()
    pendingRequests.value.clear()
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
      sendFilesList(workspaceId, requestId, path)
    })
  }

  function fetchFileContent(workspaceId: string, path: string): void {
    isLoadingContent.value = true
    const requestId = nextRequestId()
    pendingRequests.value.set(requestId, () => {
      // cleanup handled by setFileContent
    })
    sendFilesRead(workspaceId, requestId, path)
  }

  function downloadFile(workspaceId: string, path: string): void {
    const requestId = nextRequestId()
    pendingRequests.value.set(requestId, () => {})
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
    loadingPaths.value.delete(path)

    if (error) {
      console.warn('[fileExplorer] list error:', error)
      return
    }

    setTree(path, entries)
  }

  function handleContentResult(
    requestId: string,
    path: string,
    content: string,
    size: number,
    truncated: boolean,
    error?: string,
  ): void {
    // Ignore results we didn't request (e.g. from workspaceImages store)
    if (!pendingRequests.value.has(requestId)) return

    const callback = pendingRequests.value.get(requestId)
    if (callback) {
      callback(null)
      pendingRequests.value.delete(requestId)
    }

    if (error) {
      isLoadingContent.value = false
      console.warn('[fileExplorer] content error:', error)
      return
    }

    setFileContent(path, content, size, truncated)
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
      callback(null)
      pendingRequests.value.delete(requestId)
    }

    if (error || status === 'error') {
      console.warn('[fileExplorer] upload error:', error)
      const notify = useNotificationStore()
      notify.error('Upload failed', error ?? 'The file could not be uploaded.')
      return
    }

    // Refresh the parent directory
    refreshDirectory(workspaceId, path)
  }

  /**
   * Register a pending upload and return a Promise that resolves when the
   * `files:upload_result` is received for the given requestId.
   * This lets callers await upload completion for UI feedback.
   */
  function trackAndUpload(
    _workspaceId: string,
    requestId: string,
    _path: string,
    _filename: string,
    _content: string,
  ): Promise<void> {
    return new Promise<void>((resolve) => {
      pendingRequests.value.set(requestId, () => resolve())
    })
  }

  function handleDownloadResult(
    requestId: string,
    content: string,
    filename: string,
    isArchive: boolean,
    error?: string,
  ): void {
    const callback = pendingRequests.value.get(requestId)
    if (callback) {
      callback(null)
      pendingRequests.value.delete(requestId)
    }

    if (error) {
      console.warn('[fileExplorer] download error:', error)
      return
    }

    // Trigger browser download
    const raw = atob(content)
    const bytes = new Uint8Array(raw.length)
    for (let i = 0; i < raw.length; i++) {
      bytes[i] = raw.charCodeAt(i)
    }
    const mimeType = isArchive ? 'application/gzip' : 'application/octet-stream'
    const blob = new Blob([bytes], { type: mimeType })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  return {
    // state
    isOpen,
    panelWidth,
    tree,
    expandedPaths,
    selectedPath,
    viewingFile,
    isLoadingContent,
    loadingPaths,
    // getters
    isViewingFile,
    // actions
    open,
    close,
    toggle,
    setTree,
    toggleExpand,
    selectFile,
    openPath,
    setFileContent,
    closeFileViewer,
    reset,
    fetchDirectory,
    downloadFile,
    refreshAll,
    trackAndUpload,
    // event handlers
    handleListResult,
    handleContentResult,
    handleUploadResult,
    handleDownloadResult,
  }
})
