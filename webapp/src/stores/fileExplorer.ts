/**
 * File explorer store — manages the file explorer panel state,
 * tree structure, uploads, and downloads. Files open in the editor store;
 * bytes move over signed HTTP links, never through the socket.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { FileNode, FileEntryRaw } from '@/types'
import { canTransferFile, fileTransferLimitMessage } from '@opencuria/chat-core/workspaceFiles'
import type { WorkspaceFileUploadResult } from '@opencuria/client/files'
import { listWorkspaceFiles, findWorkspaceFiles } from '@/services/socket'
import { startDownload, workspaceFiles } from '@/services/workspaceFiles'
import { useWorkspaceStore } from './workspaces'
import { useNotificationStore } from '@/stores/notifications'
import { useSidePanelStore } from '@/stores/sidePanel'
import { useEditorStore } from '@/stores/editor'

export interface WorkspaceUpload {
  directory: string
  filename: string
  body: Blob
  /** `replace` overwrites an existing file; `rename` keeps it as `name_1.ext`. */
  conflict: 'rename' | 'replace'
  /** Refresh the explorer directory afterwards (default true). */
  refreshExplorer?: boolean
  signal?: AbortSignal
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true || (error instanceof Error && error.name === 'AbortError')
}

export const useFileExplorerStore = defineStore('fileExplorer', () => {
  // -- state ----------------------------------------------------------------

  const tree = ref<FileNode[]>([])
  const expandedPaths = ref<Set<string>>(new Set())
  const selectedPath = ref<string | null>(null)
  const loadingPaths = ref<Set<string>>(new Set())

  const fileControllers = new Set<AbortController>()

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
    void useEditorStore().openFile(workspaceId, path)
  }

  function reset(): void {
    for (const controller of fileControllers) controller.abort()
    fileControllers.clear()
    selectedPath.value = null
    tree.value = []
    expandedPaths.value = new Set()
    loadingPaths.value = new Set()
  }

  // -- socket request helpers -----------------------------------------------

  async function requestDirectory(
    workspaceId: string,
    path: string,
    updateTree: boolean,
    signal?: AbortSignal,
  ): Promise<FileEntryRaw[] | null> {
    const controller = new AbortController()
    fileControllers.add(controller)
    const requestSignal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal
    const loading = loadingPaths.value
    if (updateTree) loading.add(path)
    try {
      const result = await listWorkspaceFiles(workspaceId, path, requestSignal)
      if (requestSignal.aborted) return null
      if (updateTree) setTree(path, result.entries)
      return result.entries
    } catch {
      return null
    } finally {
      fileControllers.delete(controller)
      if (updateTree) loading.delete(path)
    }
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

  async function findFiles(workspaceId: string, query: string, limit = 50): Promise<string[]> {
    const controller = new AbortController()
    fileControllers.add(controller)
    try {
      const result = await findWorkspaceFiles(workspaceId, query, limit, controller.signal)
      return controller.signal.aborted ? [] : result.paths
    } catch {
      return []
    } finally {
      fileControllers.delete(controller)
    }
  }

  /** Save a file, or a folder as `.tar.gz`, straight from its signed link. */
  async function downloadFile(workspaceId: string, path: string): Promise<void> {
    if (useWorkspaceStore().isWorkspaceTransitioning(workspaceId)) return
    const controller = new AbortController()
    fileControllers.add(controller)
    try {
      const link = await workspaceFiles().link(workspaceId, path, controller.signal)
      if (controller.signal.aborted) return
      if (!canTransferFile(link.size)) throw new Error(fileTransferLimitMessage())
      startDownload(link.downloadUrl)
    } catch (error) {
      if (!controller.signal.aborted) {
        useNotificationStore().error(
          'Download failed',
          error instanceof Error ? error.message : 'Download failed. Please retry.',
        )
      }
    } finally {
      fileControllers.delete(controller)
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

  /**
   * Upload one file and refresh its explorer directory. Failures are shown
   * as a toast and resolve to `null`; cancelled uploads stay silent.
   */
  async function uploadFile(
    workspaceId: string,
    upload: WorkspaceUpload,
  ): Promise<WorkspaceFileUploadResult | null> {
    const { refreshExplorer = true, ...input } = upload
    try {
      const result = await workspaceFiles().upload(workspaceId, input)
      if (refreshExplorer) refreshDirectory(workspaceId, input.directory)
      return result
    } catch (error) {
      if (!isAbort(error, input.signal)) {
        useNotificationStore().error(
          'Upload failed',
          error instanceof Error && error.message
            ? error.message
            : 'The file could not be uploaded.',
        )
      }
      return null
    }
  }

  return {
    // state
    tree,
    expandedPaths,
    selectedPath,
    loadingPaths,
    // actions
    setTree,
    toggleExpand,
    selectFile,
    openPath,
    reset,
    fetchDirectory,
    fetchDirectoryEntries,
    findFiles,
    downloadFile,
    refreshAll,
    uploadFile,
  }
})
