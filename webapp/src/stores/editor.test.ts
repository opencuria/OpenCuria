import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useEditorStore } from './editor'
import { ApiRequestError } from '@/services/api'
import { readEditorFile, readGitFileContent, saveEditorFile } from '@/services/editor.api'
import { runGitOperation } from '@/services/git.api'
import { readWorkspaceFile } from '@/services/socket'
import { onEditorModelDisposal } from '@/lib/editorModelLifecycle'

vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(),
  readGitFileContent: vi.fn(),
  saveEditorFile: vi.fn(),
}))
vi.mock('@/services/git.api', () => ({ runGitOperation: vi.fn() }))
vi.mock('@/services/socket', () => ({ readWorkspaceFile: vi.fn() }))

const WS = 'ws-1'
const REPO = '/workspace/repo'
const FILE = `${REPO}/a.txt`
const range = { originalStart: 1, originalEnd: 2, modifiedStart: 1, modifiedEnd: 2 }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function content(path = FILE, text = 'before\n', revision = 'worktree-1') {
  return { path, content: text, revision }
}

beforeEach(() => {
  vi.resetAllMocks()
  setActivePinia(createPinia())
  vi.mocked(readEditorFile).mockImplementation(async (_ws, path) => content(path))
  vi.mocked(saveEditorFile).mockImplementation(async (_ws, path, text) =>
    content(path, text, 'worktree-2'),
  )
  vi.mocked(readGitFileContent).mockImplementation(async (_ws, _repo, _path, revision) => ({
    ok: true,
    content: revision === 'worktree' ? 'after\n' : revision === 'index' ? 'before\n' : 'head\n',
    revision: `${revision}-1`,
    exists: true,
  }))
  vi.mocked(runGitOperation).mockResolvedValue({ ok: true, snapshot: {} } as never)
})

describe('editor workspace tabs and shared buffers', () => {
  it('releases closed models after UI detachment and keeps models for tab switches', async () => {
    const dispose = vi.fn()
    const stop = onEditorModelDisposal(dispose)
    const store = useEditorStore()
    try {
      await store.openFile(WS, FILE)
      const first = store.activeTab!.id
      await store.openFile(WS, `${REPO}/b.txt`)
      const second = store.activeTab!.id
      store.activateTab(first)
      store.showChat()
      store.setWorkspace('other-workspace')
      await nextTick()
      expect(dispose).not.toHaveBeenCalled()
      expect(store.closeTab(first)).toBe(true)
      expect(dispose).not.toHaveBeenCalled()
      await nextTick()
      expect(dispose).toHaveBeenCalledExactlyOnceWith(first)
      store.reset()
      expect(dispose).toHaveBeenCalledTimes(1)
      await nextTick()
      expect(dispose).toHaveBeenLastCalledWith(second)
      expect(dispose).toHaveBeenCalledTimes(2)
    } finally {
      stop()
    }
  })

  it('does not dispose a same-tick reopened tab or a declined dirty close', async () => {
    const dispose = vi.fn()
    const stop = onEditorModelDisposal(dispose)
    const store = useEditorStore()
    try {
      await store.openFile(WS, FILE)
      const id = store.activeTab!.id
      store.closeTab(id)
      const reopen = store.openFile(WS, FILE)
      await nextTick()
      await reopen
      expect(dispose).not.toHaveBeenCalled()
      store.updateContent(id, 'draft')
      expect(store.closeTab(id)).toBe(false)
      await nextTick()
      expect(dispose).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('never reuses a logged-out document model even if the same file reopens immediately', async () => {
    const dispose = vi.fn()
    const stop = onEditorModelDisposal(dispose)
    const store = useEditorStore()
    try {
      await store.openFile(WS, FILE)
      const previousId = store.activeTab!.id
      store.reset()
      const reopen = store.openFile(WS, FILE)
      const nextId = store.activeTab!.id
      expect(nextId).not.toBe(previousId)
      await nextTick()
      await reopen
      expect(dispose).toHaveBeenCalledExactlyOnceWith(previousId)
      expect(store.activeTab!.id).toBe(nextId)
    } finally {
      stop()
    }
  })

  it('retains open tabs, active selection and unsaved edits across workspace changes', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const first = store.activeTab!.id
    store.updateContent(first, 'unsaved\n')
    await store.openFile(WS, `${REPO}/b.txt`)
    const second = store.activeTab!.id
    expect(store.tabs).toHaveLength(2)
    store.activateTab(first)
    await store.openFile('ws-2', FILE)
    expect(store.tabs).toHaveLength(1)
    expect(store.activeTab!.content).toBe('before\n')
    store.setWorkspace(WS)
    expect(store.activeTab!.id).toBe(first)
    expect(store.activeTab!.content).toBe('unsaved\n')
    expect(store.activeTab!.dirty).toBe(true)
    store.showChat()
    expect(store.isOpen).toBe(false)
    store.activateTab(second)
    expect(store.isOpen).toBe(true)
  })

  it('deduplicates tabs and does not let a late read switch the active workspace', async () => {
    const read = deferred<ReturnType<typeof content>>()
    vi.mocked(readEditorFile).mockReturnValueOnce(read.promise)
    const store = useEditorStore()
    const open = store.openFile(WS, FILE)
    await store.openFile('ws-2', `${REPO}/other.txt`)
    read.resolve(content(FILE, 'loaded later\n'))
    await open
    expect(store.workspaceId).toBe('ws-2')
    store.setWorkspace(WS)
    await store.openFile(WS, FILE)
    expect(store.tabs).toHaveLength(1)
    expect(store.activeTab!.content).toBe('loaded later\n')
    expect(readEditorFile).toHaveBeenCalledTimes(2)
  })

  it('shares the editable document with an unstaged diff without losing dirty content', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const fileId = store.activeTab!.id
    store.updateContent(fileId, 'my draft\n')
    await store.openDiff(WS, REPO, 'a.txt')
    expect(store.activeTab).toMatchObject({
      content: 'my draft\n',
      originalContent: 'before\n',
      dirty: true,
    })
    store.updateContent(store.activeTab!.id, 'shared draft\n')
    store.activateTab(fileId)
    expect(store.activeTab!.content).toBe('shared draft\n')
    expect(store.closeTab(fileId)).toBe(false)
    expect(store.pendingCloseId).toBe(fileId)
    expect(store.closeTab(fileId, true)).toBe(true)
    expect(store.activeTab!.content).toBe('shared draft\n')
  })

  it('aborts closed reads and prevents stale content from replacing a reopened tab', async () => {
    const read = deferred<ReturnType<typeof content>>()
    vi.mocked(readEditorFile).mockReturnValueOnce(read.promise)
    const store = useEditorStore()
    const oldOpen = store.openFile(WS, FILE)
    const signal = vi.mocked(readEditorFile).mock.calls[0]![2]!
    expect(store.closeTab(store.activeTab!.id)).toBe(true)
    expect(signal.aborted).toBe(true)
    await store.openFile(WS, FILE)
    read.resolve(content(FILE, 'stale'))
    await oldOpen
    expect(store.activeTab!.content).toBe('before\n')
    store.reset()
    expect(store.tabs).toEqual([])
    expect(store.hasUnsavedChanges).toBe(false)
    expect(store.workspaceId).toBeNull()
  })
})

describe('editor safe saves', () => {
  it('saves exact content with its revision and clears the dirty marker', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const id = store.activeTab!.id
    store.updateContent(id, 'Ä\r\nno final newline')
    expect(await store.saveTab(id)).toBe(true)
    expect(saveEditorFile).toHaveBeenCalledWith(WS, FILE, 'Ä\r\nno final newline', 'worktree-1')
    expect(store.activeTab).toMatchObject({ dirty: false, revision: 'worktree-2', saving: false })
  })

  it('preserves edits typed during an in-flight save and prevents closing them', async () => {
    const save = deferred<ReturnType<typeof content>>()
    vi.mocked(saveEditorFile).mockReturnValueOnce(save.promise)
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const id = store.activeTab!.id
    store.updateContent(id, 'first edit')
    const pending = store.saveTab(id)
    store.updateContent(id, 'later edit')
    expect(store.closeTab(id, true)).toBe(false)
    save.resolve(content(FILE, 'first edit', 'saved-first'))
    expect(await pending).toBe(false)
    expect(store.activeTab).toMatchObject({
      content: 'later edit',
      savedContent: 'first edit',
      dirty: true,
      revision: 'saved-first',
    })
    expect(await store.saveTab(id)).toBe(true)
    expect(saveEditorFile).toHaveBeenLastCalledWith(WS, FILE, 'later edit', 'saved-first')
  })

  it('keeps drafts on external conflicts and requires explicit discard before reload', async () => {
    vi.mocked(saveEditorFile).mockRejectedValueOnce(
      new ApiRequestError(409, 'Changed', 'stale_document'),
    )
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const id = store.activeTab!.id
    store.updateContent(id, 'my draft')
    expect(await store.saveTab(id)).toBe(false)
    expect(store.activeTab).toMatchObject({ content: 'my draft', dirty: true, conflict: true })
    expect(await store.reloadTab(id)).toBe(false)
    expect(readEditorFile).toHaveBeenCalledTimes(1)
    vi.mocked(readEditorFile).mockResolvedValueOnce(
      content(FILE, 'external edit', 'external-version'),
    )
    expect(await store.reloadTab(id, true)).toBe(true)
    expect(store.activeTab).toMatchObject({
      content: 'external edit',
      dirty: false,
      conflict: false,
    })
  })

  it('retains dirty buffers during external file refresh and retries failed reads', async () => {
    vi.mocked(readEditorFile).mockRejectedValueOnce(new Error('Runner offline'))
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const id = store.activeTab!.id
    expect(store.activeTab).toMatchObject({ error: 'Runner offline', loading: false })
    expect(await store.reloadTab(id)).toBe(true)
    store.updateContent(id, 'keep this')
    await store.refreshWorkspaceFiles(WS)
    expect(readEditorFile).toHaveBeenCalledTimes(2)
    expect(store.activeTab!.content).toBe('keep this')
  })
})

describe('editor Git diffs and partial operations', () => {
  it('loads staged and historical blobs as separate readonly tabs', async () => {
    const store = useEditorStore()
    await store.openDiff(WS, REPO, 'a.txt', true, 'old-name.txt')
    expect(store.activeTab).toMatchObject({
      originalContent: 'head\n',
      content: 'before\n',
      readOnly: true,
      staged: true,
    })
    expect(readGitFileContent).toHaveBeenCalledWith(
      WS,
      REPO,
      'old-name.txt',
      'head',
      expect.any(AbortSignal),
    )
    await store.openCommitDiff(WS, REPO, 'renamed.txt', 'abcdef', 'old.txt')
    expect(readGitFileContent).toHaveBeenCalledWith(
      WS,
      REPO,
      'old.txt',
      'abcdef^',
      expect.any(AbortSignal),
    )
    expect(readGitFileContent).toHaveBeenCalledWith(
      WS,
      REPO,
      'renamed.txt',
      'abcdef',
      expect.any(AbortSignal),
    )
    expect(store.activeTab).toMatchObject({ kind: 'commit', readOnly: true })
    expect(store.tabs).toHaveLength(2)
  })

  it('stages a hunk using both snapshot revisions and refreshes open files', async () => {
    const store = useEditorStore()
    await store.openDiff(WS, REPO, 'a.txt')
    expect(readEditorFile).not.toHaveBeenCalled()
    expect(await store.applyChange(store.activeTab!.id, 'stage', range)).toBe(true)
    expect(runGitOperation).toHaveBeenCalledWith(WS, {
      operation: 'stage_patch',
      repo_path: REPO,
      path: 'a.txt',
      patch: '@@ -1,1 +1,1 @@\n-before\n+after\n',
      expected_index_revision: 'index-1',
      expected_worktree_revision: 'worktree-1',
    })
    expect(store.activeTab!.busy).toBe(false)
  })

  it('blocks unsaved staging and uses the saved revision on a later operation', async () => {
    const store = useEditorStore()
    await store.openDiff(WS, REPO, 'a.txt')
    const id = store.activeTab!.id
    store.updateContent(id, 'draft\n')
    expect(await store.applyChange(id, 'stage', range)).toBe(false)
    expect(runGitOperation).not.toHaveBeenCalled()
    expect(await store.saveTab(id)).toBe(true)
    expect(await store.applyChange(id, 'stage', range)).toBe(true)
    expect(runGitOperation).toHaveBeenCalledWith(
      WS,
      expect.objectContaining({ expected_worktree_revision: 'worktree-2' }),
    )
  })

  it('unstages selected staged hunks and surfaces stale patch errors', async () => {
    const store = useEditorStore()
    await store.openDiff(WS, REPO, 'a.txt', true)
    const id = store.activeTab!.id
    vi.mocked(runGitOperation).mockRejectedValueOnce(
      new ApiRequestError(409, 'Changed', 'stale_document'),
    )
    expect(await store.applyChange(id, 'stage', range)).toBe(false)
    expect(runGitOperation).toHaveBeenCalledWith(
      WS,
      expect.objectContaining({ operation: 'unstage_patch' }),
    )
    expect(store.activeTab).toMatchObject({ conflict: true, busy: false })
    expect(await store.applyChange(id, 'revert', range)).toBe(false)
  })

  it('guards staged renamed hunks with the original HEAD token and refreshes it after reload', async () => {
    const originalRead = vi.mocked(readGitFileContent).getMockImplementation()!
    let headRevision = 'head-before-reset'
    vi.mocked(readGitFileContent).mockImplementation(async (...args) => {
      const result = await originalRead(...args)
      return args[3] === 'head' ? { ...result, revision: headRevision } : result
    })
    const store = useEditorStore()
    await store.openDiff(WS, REPO, 'renamed.txt', true, 'old-name.txt')
    const id = store.activeTab!.id
    expect(store.activeTab).toMatchObject({ headRevision: 'head-before-reset' })

    // A soft reset can change HEAD while index and worktree content stay equal.
    headRevision = 'head-after-reset'
    vi.mocked(runGitOperation).mockRejectedValueOnce(
      new ApiRequestError(409, 'HEAD changed', 'stale_document'),
    )
    expect(await store.applyChange(id, 'stage', range)).toBe(false)
    expect(runGitOperation).toHaveBeenLastCalledWith(
      WS,
      expect.objectContaining({
        operation: 'unstage_patch',
        path: 'renamed.txt',
        old_path: 'old-name.txt',
        expected_head_revision: 'head-before-reset',
        expected_index_revision: 'index-1',
        expected_worktree_revision: 'worktree-1',
      }),
    )
    expect(store.activeTab).toMatchObject({ conflict: true })
    expect(await store.reloadTab(id)).toBe(true)
    expect(store.activeTab).toMatchObject({ headRevision: 'head-after-reset', conflict: false })
    expect(await store.applyChange(id, 'stage', range)).toBe(true)
    expect(runGitOperation).toHaveBeenLastCalledWith(
      WS,
      expect.objectContaining({ expected_head_revision: 'head-after-reset' }),
    )
  })
})

describe('editor previews', () => {
  it('opens SVG as editable source and shares it with a working-tree diff', async () => {
    const path = `${REPO}/icon.svg`
    const original = '<svg><path d="M0 0" /></svg>\n'
    const modified = '<svg><path d="M1 1" /></svg>\n'
    vi.mocked(readEditorFile).mockResolvedValueOnce({
      path,
      content: modified,
      revision: 'svg-worktree',
    })
    vi.mocked(readGitFileContent).mockImplementation(async (_ws, _repo, _path, revision) => ({
      ok: true,
      content: revision === 'index' ? original : modified,
      revision: `svg-${revision}`,
      exists: true,
    }))
    const store = useEditorStore()
    await store.openFile(WS, path)
    const fileId = store.activeTab!.id
    expect(store.activeTab).toMatchObject({
      mediaType: 'text',
      readOnly: false,
      content: modified,
    })
    expect(readWorkspaceFile).not.toHaveBeenCalled()
    await store.openDiff(WS, REPO, 'icon.svg')
    expect(store.activeTab).toMatchObject({
      mediaType: 'text',
      readOnly: false,
      content: modified,
      originalContent: original,
    })
    store.updateContent(store.activeTab!.id, '<svg />\n')
    expect(store.activeTab!.dirty).toBe(true)
    store.activateTab(fileId)
    expect(store.activeTab!.content).toBe('<svg />\n')
  })

  it('keeps image and PDF previews without requesting editable text', async () => {
    vi.mocked(readWorkspaceFile).mockResolvedValue({
      workspace_id: WS,
      request_id: 'read',
      path: '/workspace/image.png',
      content: 'AQID',
      size: 3,
      truncated: false,
    })
    const store = useEditorStore()
    await store.openFile(WS, '/workspace/image.png')
    expect(readEditorFile).not.toHaveBeenCalled()
    expect(store.activeTab).toMatchObject({
      mediaType: 'image',
      mimeType: 'image/png',
      rawBase64: 'AQID',
      readOnly: true,
      previewBytes: 3,
    })
    store.updateContent(store.activeTab!.id, 'bad overwrite')
    expect(store.activeTab!.dirty).toBe(false)
    expect(await store.saveTab(store.activeTab!.id)).toBe(false)
  })

  it('allows only readonly preview for oversized text and never saves truncated bytes', async () => {
    vi.mocked(readEditorFile).mockRejectedValueOnce(
      new ApiRequestError(413, 'Too large', 'file_too_large'),
    )
    vi.mocked(readWorkspaceFile).mockResolvedValue({
      workspace_id: WS,
      request_id: 'read',
      path: FILE,
      content: btoa('preview'),
      size: 9_000_000,
      truncated: true,
    })
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    expect(store.activeTab).toMatchObject({
      mediaType: 'text',
      content: 'preview',
      truncated: true,
      readOnly: true,
    })
    expect(await store.saveTab(store.activeTab!.id)).toBe(false)
    expect(saveEditorFile).not.toHaveBeenCalled()
  })
})
