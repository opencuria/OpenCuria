import { defineStore } from 'pinia'
import { computed, nextTick, ref } from 'vue'
import { ApiRequestError } from '@/services/api'
import { readEditorFile, readGitFileContent, saveEditorFile } from '@/services/editor.api'
import { runGitOperation } from '@/services/git.api'
import { readWorkspaceFile } from '@/services/socket'
import { buildMonacoChangePatch, type MonacoChangeRange } from '@/lib/monacoChanges'
import { disposeEditorModel } from '@/lib/editorModelLifecycle'

export type EditorTabKind = 'file' | 'diff' | 'commit'
export type EditorMediaType = 'text' | 'image' | 'pdf' | 'binary'

interface EditorDocument {
  workspaceId: string
  path: string
  content: string
  savedContent: string
  revision: string | null
  loaded: boolean
  loading: boolean
  saving: boolean
  error: string | null
  conflict: boolean
  readOnly: boolean
  mediaType: EditorMediaType
  mimeType: string
  rawBase64: string
  size: number
  previewBytes: number
  truncated: boolean
  isBinary: boolean
}

interface EditorTabState {
  id: string
  workspaceId: string
  kind: EditorTabKind
  path: string
  name: string
  repoPath: string | null
  staged: boolean
  commitHash: string | null
  oldPath: string
  documentKey: string | null
  content: string
  originalContent: string
  indexRevision: string | null
  worktreeRevision: string | null
  headRevision: string | null
  loaded: boolean
  loading: boolean
  busy: boolean
  error: string | null
  conflict: boolean
}

export interface EditorTab extends Omit<EditorTabState, 'documentKey'> {
  content: string
  savedContent: string
  revision: string | null
  dirty: boolean
  saving: boolean
  readOnly: boolean
  mediaType: EditorMediaType
  mimeType: string
  rawBase64: string
  size: number
  previewBytes: number
  truncated: boolean
  isBinary: boolean
}

const IMAGE_TYPES: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  bmp: 'image/bmp',
  ico: 'image/x-icon',
  tiff: 'image/tiff',
  tif: 'image/tiff',
  avif: 'image/avif',
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unable to load the file. Please retry.'
}

function isStaleDocument(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 409 && error.code === 'stale_document'
}

function documentKey(workspaceId: string, path: string): string {
  return JSON.stringify([workspaceId, path])
}

function absolutePath(repoPath: string, path: string): string {
  return `${repoPath.replace(/\/$/, '')}/${path}`
}

let modelGeneration = 0

/** Buffers belong to documents, so file and working-tree diff tabs share edits. */
export const useEditorStore = defineStore('editor', () => {
  let generation = ++modelGeneration
  const workspaceId = ref<string | null>(null)
  const allTabs = ref<EditorTabState[]>([])
  const documents = ref<Record<string, EditorDocument>>({})
  const activeIds = ref<Record<string, string | null>>({})
  const visible = ref<Record<string, boolean>>({})
  const pendingCloseId = ref<string | null>(null)

  const documentLoads = new Map<string, Promise<boolean>>()
  const documentSaves = new Map<string, Promise<boolean>>()
  const documentControllers = new Map<string, AbortController>()
  const tabLoads = new Map<string, Promise<boolean>>()
  const tabControllers = new Map<string, AbortController>()

  function stateFor(id: string): EditorTabState | undefined {
    return allTabs.value.find((tab) => tab.id === id)
  }

  function releaseClosedModels(ids: string[]): void {
    // Vue first unmounts/detaches the editor. A same-tick reopen keeps its model.
    void nextTick(() => {
      for (const id of ids) {
        if (!stateFor(id)) disposeEditorModel(id)
      }
    })
  }

  function documentFor(tab: EditorTabState): EditorDocument | undefined {
    return tab.documentKey ? documents.value[tab.documentKey] : undefined
  }

  function project(tab: EditorTabState): EditorTab {
    const document = documentFor(tab)
    return {
      ...tab,
      content: document?.content ?? tab.content,
      originalContent: tab.kind === 'file' ? (document?.savedContent ?? '') : tab.originalContent,
      savedContent: document?.savedContent ?? tab.content,
      revision: document?.revision ?? null,
      dirty: !!document && document.content !== document.savedContent,
      loaded: tab.kind === 'file' ? !!document?.loaded : tab.loaded,
      loading: tab.loading || !!document?.loading,
      saving: !!document?.saving,
      busy: tab.busy,
      error: tab.error ?? document?.error ?? null,
      conflict: tab.conflict || !!document?.conflict,
      readOnly: document ? document.readOnly : true,
      mediaType: document?.mediaType ?? 'text',
      mimeType: document?.mimeType ?? 'text/plain',
      rawBase64: document?.rawBase64 ?? '',
      size: document?.size ?? 0,
      previewBytes: document?.previewBytes ?? 0,
      truncated: document?.truncated ?? false,
      isBinary: document?.isBinary ?? false,
    }
  }

  const tabs = computed(() =>
    allTabs.value.filter((tab) => tab.workspaceId === workspaceId.value).map(project),
  )
  const activeTab = computed(() => {
    const id = workspaceId.value ? activeIds.value[workspaceId.value] : null
    return tabs.value.find((tab) => tab.id === id) ?? null
  })
  const isOpen = computed(
    () => !!workspaceId.value && !!visible.value[workspaceId.value] && !!activeTab.value,
  )
  const hasUnsavedChanges = computed(() =>
    Object.values(documents.value).some((document) => document.content !== document.savedContent),
  )

  function setWorkspace(id: string | null): void {
    if (workspaceId.value !== id) pendingCloseId.value = null
    workspaceId.value = id
  }

  function activateTab(id: string): void {
    const tab = stateFor(id)
    if (!tab) return
    workspaceId.value = tab.workspaceId
    activeIds.value[tab.workspaceId] = id
    visible.value[tab.workspaceId] = true
  }

  function showChat(): void {
    if (workspaceId.value) visible.value[workspaceId.value] = false
  }

  function getDocument(id: string, path: string): { key: string; document: EditorDocument } {
    const key = documentKey(id, path)
    if (!documents.value[key]) {
      documents.value[key] = {
        workspaceId: id,
        path,
        content: '',
        savedContent: '',
        revision: null,
        loaded: false,
        loading: false,
        saving: false,
        error: null,
        conflict: false,
        readOnly: false,
        mediaType: 'text',
        mimeType: 'text/plain',
        rawBase64: '',
        size: 0,
        previewBytes: 0,
        truncated: false,
        isBinary: false,
      }
    }
    return { key, document: documents.value[key]! }
  }

  async function loadPreview(document: EditorDocument, signal: AbortSignal): Promise<void> {
    const result = await readWorkspaceFile(document.workspaceId, document.path, undefined, signal)
    if (signal.aborted) return
    const bytes = Uint8Array.from(atob(result.content.replace(/\s+/g, '')), (c) => c.charCodeAt(0))
    const extension = document.path.split('.').pop()?.toLowerCase() ?? ''
    const imageMime = IMAGE_TYPES[extension]
    let content = ''
    let binary = !!imageMime || extension === 'pdf'
    if (!binary) {
      try {
        binary = bytes.includes(0)
        if (!binary) content = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
      } catch {
        binary = true
      }
    }
    document.content = content
    document.savedContent = content
    document.revision = null
    document.readOnly = true
    document.mediaType = imageMime
      ? 'image'
      : extension === 'pdf'
        ? 'pdf'
        : binary
          ? 'binary'
          : 'text'
    document.mimeType =
      imageMime ?? (extension === 'pdf' ? 'application/pdf' : (result.mime_type ?? 'text/plain'))
    document.rawBase64 = result.content
    document.size = result.size
    document.previewBytes = bytes.length
    document.truncated = result.truncated
    document.isBinary = binary
  }

  function loadDocument(key: string, discardChanges = false): Promise<boolean> {
    const document = documents.value[key]
    if (!document) return Promise.resolve(false)
    const pending = documentLoads.get(key)
    if (pending) return pending
    if (document.content !== document.savedContent && !discardChanges) return Promise.resolve(false)
    const controller = new AbortController()
    documentControllers.set(key, controller)
    document.loading = true
    document.error = null
    const run = (async (): Promise<boolean> => {
      try {
        const extension = document.path.split('.').pop()?.toLowerCase() ?? ''
        if (IMAGE_TYPES[extension] || extension === 'pdf') {
          await loadPreview(document, controller.signal)
        } else {
          try {
            const result = await readEditorFile(
              document.workspaceId,
              document.path,
              controller.signal,
            )
            if (controller.signal.aborted || documents.value[key] !== document) return false
            document.content = result.content
            document.savedContent = result.content
            document.revision = result.revision
            document.size = new TextEncoder().encode(result.content).length
            document.previewBytes = document.size
            document.readOnly = false
            document.truncated = false
            document.rawBase64 = ''
            document.mediaType = 'text'
            document.mimeType = 'text/plain'
            document.isBinary = false
          } catch (error) {
            if (
              !(error instanceof ApiRequestError) ||
              !['binary_file', 'file_too_large'].includes(error.code ?? '')
            )
              throw error
            await loadPreview(document, controller.signal)
          }
        }
        if (controller.signal.aborted || documents.value[key] !== document) return false
        document.loaded = true
        document.conflict = false
        return true
      } catch (error) {
        if (!controller.signal.aborted) document.error = errorMessage(error)
        return false
      } finally {
        if (documentControllers.get(key) === controller) {
          document.loading = false
          documentControllers.delete(key)
          documentLoads.delete(key)
        }
      }
    })()
    documentLoads.set(key, run)
    return run
  }

  function newTab(
    id: string,
    wsId: string,
    kind: EditorTabKind,
    path: string,
    repoPath: string | null = null,
    staged = false,
    commitHash: string | null = null,
    oldPath = path,
  ): EditorTabState {
    const existing = stateFor(id)
    if (existing) return existing
    const document =
      kind === 'file' || (kind === 'diff' && !staged)
        ? getDocument(wsId, repoPath ? absolutePath(repoPath, path) : path)
        : null
    allTabs.value.push({
      id,
      workspaceId: wsId,
      kind,
      path,
      name: path.split('/').pop() || path,
      repoPath,
      staged,
      commitHash,
      oldPath,
      documentKey: document?.key ?? null,
      content: '',
      originalContent: '',
      indexRevision: null,
      worktreeRevision: null,
      headRevision: null,
      loaded: false,
      loading: false,
      busy: false,
      error: null,
      conflict: false,
    })
    return stateFor(id)!
  }

  async function openFile(wsId: string, path: string): Promise<void> {
    const tab = newTab(JSON.stringify([generation, wsId, 'file', path]), wsId, 'file', path)
    activateTab(tab.id)
    const document = documentFor(tab)
    if (document && !document.loaded) await loadDocument(tab.documentKey!)
  }

  function loadDiff(tab: EditorTabState): Promise<boolean> {
    const pending = tabLoads.get(tab.id)
    if (pending) return pending
    if (!tab.repoPath) return Promise.resolve(false)
    const controller = new AbortController()
    tabControllers.set(tab.id, controller)
    tab.loading = true
    tab.error = null
    const run = (async (): Promise<boolean> => {
      try {
        if (tab.kind === 'commit') {
          const [original, modified] = await Promise.all([
            readGitFileContent(
              tab.workspaceId,
              tab.repoPath!,
              tab.oldPath,
              `${tab.commitHash}^`,
              controller.signal,
            ),
            readGitFileContent(
              tab.workspaceId,
              tab.repoPath!,
              tab.path,
              tab.commitHash!,
              controller.signal,
            ),
          ])
          if (controller.signal.aborted || stateFor(tab.id) !== tab) return false
          tab.originalContent = original.content
          tab.content = modified.content
        } else {
          const document = documentFor(tab)
          const [index, worktree, head] = await Promise.all([
            readGitFileContent(
              tab.workspaceId,
              tab.repoPath!,
              tab.path,
              'index',
              controller.signal,
            ),
            readGitFileContent(
              tab.workspaceId,
              tab.repoPath!,
              tab.path,
              'worktree',
              controller.signal,
            ),
            tab.staged
              ? readGitFileContent(
                  tab.workspaceId,
                  tab.repoPath!,
                  tab.oldPath,
                  'head',
                  controller.signal,
                )
              : Promise.resolve(null),
          ])
          if (controller.signal.aborted || stateFor(tab.id) !== tab) return false
          tab.originalContent = tab.staged ? head!.content : index.content
          tab.content = tab.staged ? index.content : worktree.content
          tab.indexRevision = index.revision
          tab.worktreeRevision = worktree.revision
          tab.headRevision = head?.revision ?? null
          // A clean shared buffer follows the exact worktree snapshot used here.
          if (
            document &&
            document.content === document.savedContent &&
            !document.loading &&
            !document.saving &&
            !document.readOnly
          ) {
            document.content = worktree.content
            document.savedContent = worktree.content
            document.revision = worktree.revision
            document.loaded = true
            document.error = null
            document.conflict = false
            document.size = new TextEncoder().encode(worktree.content).length
            document.previewBytes = document.size
          }
        }
        tab.loaded = true
        tab.conflict = false
        return true
      } catch (error) {
        if (!controller.signal.aborted) tab.error = errorMessage(error)
        return false
      } finally {
        if (tabControllers.get(tab.id) === controller) {
          tab.loading = false
          tabControllers.delete(tab.id)
          tabLoads.delete(tab.id)
        }
      }
    })()
    tabLoads.set(tab.id, run)
    return run
  }

  async function openDiff(
    wsId: string,
    repoPath: string,
    path: string,
    staged = false,
    oldPath = path,
  ): Promise<void> {
    const tab = newTab(
      JSON.stringify([generation, wsId, 'diff', repoPath, path, staged]),
      wsId,
      'diff',
      path,
      repoPath,
      staged,
      null,
      oldPath,
    )
    tab.oldPath = oldPath
    activateTab(tab.id)
    await loadDiff(tab)
  }

  async function openCommitDiff(
    wsId: string,
    repoPath: string,
    path: string,
    commitHash: string,
    oldPath = path,
  ): Promise<void> {
    const tab = newTab(
      JSON.stringify([generation, wsId, 'commit', repoPath, path, commitHash]),
      wsId,
      'commit',
      path,
      repoPath,
      false,
      commitHash,
      oldPath,
    )
    activateTab(tab.id)
    if (!tab.loaded) await loadDiff(tab)
  }

  function updateContent(id: string, content: string): void {
    const tab = stateFor(id)
    const document = tab ? documentFor(tab) : undefined
    if (!document || !document.loaded || document.loading || document.readOnly) return
    document.content = content
  }

  function saveTab(id: string): Promise<boolean> {
    const tab = stateFor(id)
    const document = tab ? documentFor(tab) : undefined
    if (
      !tab ||
      !document ||
      document.readOnly ||
      !document.loaded ||
      document.loading ||
      !document.revision
    )
      return Promise.resolve(false)
    if (document.content === document.savedContent) return Promise.resolve(true)
    const key = tab.documentKey!
    const pending = documentSaves.get(key)
    if (pending) return pending
    const content = document.content
    const revision = document.revision
    document.saving = true
    document.error = null
    const run = (async (): Promise<boolean> => {
      try {
        const result = await saveEditorFile(document.workspaceId, document.path, content, revision)
        if (documents.value[key] !== document) return false
        document.savedContent = result.content
        document.revision = result.revision
        document.conflict = false
        document.size = new TextEncoder().encode(result.content).length
        for (const sibling of allTabs.value) {
          if (sibling.workspaceId !== document.workspaceId) continue
          if (
            sibling.documentKey === key ||
            (sibling.repoPath && absolutePath(sibling.repoPath, sibling.path) === document.path)
          ) {
            sibling.worktreeRevision = result.revision
            sibling.error = null
            sibling.conflict = false
          }
        }
        // Keep edits made while this save was in flight.
        return document.content === document.savedContent
      } catch (error) {
        document.conflict = isStaleDocument(error)
        document.error = document.conflict
          ? 'The file changed on disk. Reload it before saving to avoid overwriting those changes.'
          : errorMessage(error)
        return false
      } finally {
        if (documents.value[key] === document) {
          document.saving = false
          documentSaves.delete(key)
        }
      }
    })()
    documentSaves.set(key, run)
    return run
  }

  async function reloadTab(id: string, discardChanges = false): Promise<boolean> {
    const tab = stateFor(id)
    if (!tab || tab.busy) return false
    const document = documentFor(tab)
    if (document?.saving) return false
    if (document && document.content !== document.savedContent && !discardChanges) return false
    if (document && !(await loadDocument(tab.documentKey!, discardChanges))) return false
    return tab.kind === 'file' ? true : loadDiff(tab)
  }

  function closeTab(id: string, force = false): boolean {
    const tab = stateFor(id)
    if (!tab || tab.busy || documentFor(tab)?.saving) return false
    if (project(tab).dirty && !force) {
      pendingCloseId.value = id
      return false
    }
    const siblings = allTabs.value.filter((candidate) => candidate.workspaceId === tab.workspaceId)
    const index = siblings.findIndex((candidate) => candidate.id === id)
    allTabs.value = allTabs.value.filter((candidate) => candidate.id !== id)
    releaseClosedModels([id])
    tabControllers.get(id)?.abort()
    tabControllers.delete(id)
    tabLoads.delete(id)
    if (activeIds.value[tab.workspaceId] === id) {
      activeIds.value[tab.workspaceId] = siblings[index + 1]?.id ?? siblings[index - 1]?.id ?? null
    }
    if (pendingCloseId.value === id) pendingCloseId.value = null
    const key = tab.documentKey
    if (key && !allTabs.value.some((candidate) => candidate.documentKey === key)) {
      documentControllers.get(key)?.abort()
      documentControllers.delete(key)
      documentLoads.delete(key)
      delete documents.value[key]
    }
    return true
  }

  /** Refresh clean buffers only; unsaved documents survive workspace file events. */
  async function refreshWorkspaceFiles(wsId: string, paths?: string[]): Promise<void> {
    const matching = Object.entries(documents.value).filter(
      ([, document]) =>
        document.workspaceId === wsId &&
        (!paths || paths.includes(document.path)) &&
        document.content === document.savedContent &&
        !document.saving,
    )
    await Promise.all(matching.map(([key]) => loadDocument(key)))
    await Promise.all(
      allTabs.value
        .filter(
          (tab) =>
            tab.workspaceId === wsId &&
            tab.kind === 'diff' &&
            (!paths || paths.includes(absolutePath(tab.repoPath!, tab.path))),
        )
        .map((tab) => loadDiff(tab)),
    )
  }

  async function applyChange(
    id: string,
    action: 'stage' | 'revert',
    range: MonacoChangeRange,
  ): Promise<boolean> {
    const tab = stateFor(id)
    if (!tab || tab.kind !== 'diff' || !tab.loaded || tab.loading || tab.busy || !tab.repoPath)
      return false
    const document = documentFor(tab)
    if (document && (document.content !== document.savedContent || document.saving)) {
      tab.error = 'Save the file before staging or reverting changes.'
      return false
    }
    if (tab.staged && action === 'revert') return false
    if (!tab.indexRevision || !tab.worktreeRevision) return false
    if (tab.staged && !tab.headRevision) return false
    const operation = tab.staged ? 'unstage' : action === 'revert' ? 'discard' : 'stage'
    const patch = buildMonacoChangePatch(
      tab.originalContent,
      project(tab).content,
      range,
      operation,
    )
    if (!patch) return false
    tab.busy = true
    tab.error = null
    try {
      await runGitOperation(tab.workspaceId, {
        operation: `${operation}_patch`,
        repo_path: tab.repoPath,
        path: tab.path,
        patch,
        expected_index_revision: tab.indexRevision,
        expected_worktree_revision: tab.worktreeRevision,
        ...(tab.staged
          ? { expected_head_revision: tab.headRevision!, old_path: tab.oldPath }
          : {}),
      })
      await refreshWorkspaceFiles(tab.workspaceId)
      return true
    } catch (error) {
      tab.conflict = isStaleDocument(error)
      tab.error = tab.conflict
        ? 'The file or Git state changed. Reload the diff before trying again.'
        : errorMessage(error)
      return false
    } finally {
      tab.busy = false
    }
  }

  function reset(): void {
    const closedIds = allTabs.value.map((tab) => tab.id)
    generation = ++modelGeneration
    for (const controller of documentControllers.values()) controller.abort()
    for (const controller of tabControllers.values()) controller.abort()
    documentControllers.clear()
    documentLoads.clear()
    documentSaves.clear()
    tabControllers.clear()
    tabLoads.clear()
    allTabs.value = []
    documents.value = {}
    activeIds.value = {}
    visible.value = {}
    pendingCloseId.value = null
    workspaceId.value = null
    releaseClosedModels(closedIds)
  }

  return {
    workspaceId,
    tabs,
    activeTab,
    isOpen,
    hasUnsavedChanges,
    pendingCloseId,
    setWorkspace,
    activateTab,
    showChat,
    openFile,
    openDiff,
    openCommitDiff,
    updateContent,
    saveTab,
    reloadTab,
    closeTab,
    refreshWorkspaceFiles,
    applyChange,
    reset,
  }
})
