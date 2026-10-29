import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick, ref } from 'vue'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import EditorTabs from './EditorTabs.vue'
import WorkspaceEditor from './WorkspaceEditor.vue'
import { useEditorStore } from '@/stores/editor'
import { ApiRequestError } from '@/services/api'
import { readEditorFile, readGitFileContent, saveEditorFile } from '@/services/editor.api'
import { readWorkspaceFile } from '@/services/socket'

enableAutoUnmount(afterEach)

const mocks = vi.hoisted(() => ({
  refreshGit: vi.fn().mockResolvedValue(undefined),
  downloadFile: vi.fn(),
}))

vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(),
  readGitFileContent: vi.fn(),
  saveEditorFile: vi.fn(),
}))
vi.mock('@/services/git.api', () => ({ runGitOperation: vi.fn() }))
vi.mock('@/services/socket', () => ({ readWorkspaceFile: vi.fn() }))
vi.mock('@/lib/monaco', () => ({ disposeMonacoModel: vi.fn(), disposeMonacoModels: vi.fn() }))
vi.mock('./MonacoEditor.vue', () => ({
  __esModule: true,
  default: {
    name: 'MonacoEditor',
    props: [
      'modelValue',
      'original',
      'path',
      'modelKey',
      'readOnly',
      'diff',
      'staged',
      'gitActionsEnabled',
      'busy',
    ],
    emits: ['update:modelValue', 'save', 'stage', 'discard'],
    template: '<div data-testid="monaco-stub" />',
  },
}))
vi.mock('@/components/ui/sidebar', () => ({
  SidebarTrigger: { template: '<button aria-label="Toggle sidebar" />' },
}))
vi.mock('@/components/files/WorkspaceFileIcon.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/chat/SidePanelToggle.vue', () => ({
  default: {
    name: 'SidePanelToggle',
    props: ['open'],
    emits: ['toggle'],
    template: '<button aria-label="Toggle tools" @click="$emit(\'toggle\')" />',
  },
}))
vi.mock('@/components/ui/dialog', () => {
  const content = { template: '<div><slot /></div>' }
  return {
    Dialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' },
    DialogContent: content,
    DialogDescription: content,
    DialogFooter: content,
    DialogHeader: content,
    DialogTitle: content,
  }
})
vi.mock('@/stores/fileExplorer', () => ({
  useFileExplorerStore: () => ({
    downloadFile: mocks.downloadFile,
    formatBytes: (n: number) => `${n} bytes`,
  }),
}))
vi.mock('@/stores/git', () => ({ useGitStore: () => ({ refresh: mocks.refreshGit }) }))

const WS = 'editor-ui-workspace'
const FILE = '/workspace/repo/a.ts'
const frame = defineComponent({
  components: { EditorTabs, WorkspaceEditor },
  setup: () => ({ editor: ref<InstanceType<typeof WorkspaceEditor>>() }),
  template:
    '<EditorTabs @save="editor?.save()" @reload="editor?.reload()" @download="editor?.download()" /><WorkspaceEditor ref="editor" />',
})

function buttons(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find((button) => button.text() === label)!
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(readEditorFile).mockImplementation(async (_workspace, path) => ({
    path,
    content: 'before\n',
    revision: 'revision-1',
  }))
  vi.mocked(saveEditorFile).mockImplementation(async (_workspace, path, content) => ({
    path,
    content,
    revision: 'revision-2',
  }))
  vi.mocked(readGitFileContent).mockImplementation(async (_workspace, _repo, _path, revision) => ({
    ok: true,
    content: revision === 'worktree' ? 'after\n' : 'before\n',
    revision: `${revision}-revision`,
    exists: true,
  }))
  vi.mocked(readWorkspaceFile).mockResolvedValue({
    content: btoa('preview'),
    size: 7,
    truncated: false,
    mime_type: 'image/png',
  } as never)
})

describe('editor tabs and unsaved-close dialog', () => {
  it('switches files with accessible tabs and shows the dirty dot in place of the close icon', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const first = store.activeTab!.id
    await store.openFile(WS, '/workspace/repo/b.ts')
    store.updateContent(first, 'draft\n')
    const wrapper = mount(frame)
    await flushPromises()
    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((tab) => tab.text())).toEqual(['a.ts', 'b.ts'])
    expect(tabs[1]!.attributes('aria-selected')).toBe('true')
    expect(wrapper.findAll('[data-testid="editor-tab-dirty"]')).toHaveLength(1)
    expect(wrapper.find('[data-dirty="true"]').attributes('aria-label')).toBe(
      'Close a.ts (unsaved changes)',
    )
    await tabs[1]!.trigger('keydown', { key: 'ArrowLeft' })
    expect(store.activeTab!.id).toBe(first)
    expect(wrapper.find('[role="tab"][aria-selected="true"]').text()).toBe('a.ts')
    expect(wrapper.findComponent({ name: 'MonacoEditor' }).props('modelValue')).toBe('draft\n')
  })

  it('saves a dirty file before closing it and refreshes Git', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    store.updateContent(store.activeTab!.id, 'saved draft\n')
    const wrapper = mount(frame)
    await flushPromises()
    await wrapper.find('[data-testid="editor-tab-close"]').trigger('click')
    expect(wrapper.find('[data-testid="editor-unsaved-dialog"]').text()).toContain(
      'Save changes to a.ts?',
    )
    await buttons(wrapper, 'Save').trigger('click')
    await flushPromises()
    expect(saveEditorFile).toHaveBeenCalledWith(WS, FILE, 'saved draft\n', 'revision-1')
    expect(store.tabs).toHaveLength(0)
    expect(wrapper.find('[data-testid="editor-unsaved-dialog"]').exists()).toBe(false)
    expect(mocks.refreshGit).toHaveBeenCalledWith({ silent: true, forceDetails: true })
  })

  it('discards only after choosing Don’t save, and Cancel retains the draft', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    store.updateContent(store.activeTab!.id, 'draft\n')
    const wrapper = mount(frame)
    await flushPromises()
    await wrapper.find('[data-testid="editor-tab-close"]').trigger('click')
    await buttons(wrapper, 'Cancel').trigger('click')
    expect(store.activeTab!.content).toBe('draft\n')
    expect(store.pendingCloseId).toBeNull()
    await wrapper.find('[data-testid="editor-tab-close"]').trigger('click')
    await buttons(wrapper, "Don't save").trigger('click')
    expect(store.tabs).toHaveLength(0)
    expect(saveEditorFile).not.toHaveBeenCalled()
  })

  it('keeps the dialog and draft when saving fails', async () => {
    vi.mocked(saveEditorFile).mockRejectedValueOnce(new Error('Disk full'))
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    store.updateContent(store.activeTab!.id, 'draft\n')
    const wrapper = mount(frame)
    await flushPromises()
    await wrapper.find('[data-testid="editor-tab-close"]').trigger('click')
    await buttons(wrapper, 'Save').trigger('click')
    await flushPromises()
    expect(store.activeTab!.content).toBe('draft\n')
    expect(wrapper.find('[data-testid="editor-unsaved-dialog"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="editor-save-error"]').text()).toContain('Disk full')
  })

  it('emits chat toggles without hiding the editor or changing the active draft', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    store.updateContent(store.activeTab!.id, 'draft\n')
    const activeId = store.activeTab!.id
    const wrapper = mount(EditorTabs)
    const open = wrapper.find('[aria-label="Open chat"]')
    expect(open.attributes('aria-pressed')).toBe('false')
    await open.trigger('click')
    expect(wrapper.emitted('toggle-chat')).toHaveLength(1)
    expect(store.isOpen).toBe(true)
    expect(store.activeTab).toMatchObject({ id: activeId, content: 'draft\n', dirty: true })
    await wrapper.setProps({ chatOpen: true })
    const close = wrapper.find('[aria-label="Close chat"]')
    expect(close.attributes('aria-pressed')).toBe('true')
    await close.trigger('click')
    expect(wrapper.emitted('toggle-chat')).toHaveLength(2)
  })

  it('reflects the visible sidebar and delegates its toggle to the parent', async () => {
    const wrapper = mount(EditorTabs)
    const toggle = wrapper.findComponent({ name: 'SidePanelToggle' })
    expect(toggle.props('open')).toBe(false)
    await wrapper.setProps({ sidePanelOpen: true, chatOpen: true })
    expect(toggle.props('open')).toBe(true)
    await toggle.trigger('click')
    expect(wrapper.emitted('toggle-side-panel')).toHaveLength(1)
  })
})

describe('workspace editor content states and commands', () => {
  it('keeps file commands in the tab strip and runs them against the active tab', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    const fileId = store.activeTab!.id
    const wrapper = mount(frame)
    await flushPromises()
    const tabs = wrapper.find('[data-testid="editor-tabs"]')
    const editor = wrapper.find('[data-testid="workspace-editor"]')
    expect(editor.find('[aria-label="Save file"]').exists()).toBe(false)
    expect(editor.find('[aria-label="Reload from disk"]').exists()).toBe(false)
    expect(editor.find('[aria-label="Download file"]').exists()).toBe(false)
    const saveButton = tabs.find('[aria-label="Save file"]')
    expect(saveButton.attributes('disabled')).toBeDefined()
    store.updateContent(fileId, 'saved from toolbar\n')
    await nextTick()
    expect(saveButton.attributes('disabled')).toBeUndefined()
    await saveButton.trigger('click')
    await flushPromises()
    expect(saveEditorFile).toHaveBeenCalledWith(WS, FILE, 'saved from toolbar\n', 'revision-1')
    expect(store.activeTab?.dirty).toBe(false)
    await tabs.find('[aria-label="Download file"]').trigger('click')
    expect(mocks.downloadFile).toHaveBeenLastCalledWith(WS, FILE)
    await store.openDiff(WS, '/workspace/repo', 'nested/b.ts')
    await nextTick()
    await tabs.find('[aria-label="Download file"]').trigger('click')
    expect(mocks.downloadFile).toHaveBeenLastCalledWith(WS, '/workspace/repo/nested/b.ts')
    store.activateTab(fileId)
    await nextTick()
    await tabs.find('[aria-label="Reload from disk"]').trigger('click')
    await flushPromises()
    expect(store.activeTab?.content).toBe('before\n')
  })

  it('renders a loading state and supports retry after a read failure', async () => {
    let finish!: (value: { path: string; content: string; revision: string }) => void
    vi.mocked(readEditorFile).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve
      }),
    )
    const store = useEditorStore()
    const pending = store.openFile(WS, FILE)
    const wrapper = mount(WorkspaceEditor)
    expect(wrapper.find('[data-testid="editor-loading"]').exists()).toBe(true)
    finish({ path: FILE, content: 'loaded\n', revision: 'loaded-revision' })
    await pending
    await flushPromises()
    expect(wrapper.find('[data-testid="editor-loading"]').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'MonacoEditor' }).props('modelValue')).toBe('loaded\n')
    vi.mocked(readEditorFile).mockRejectedValueOnce(new Error('Runner disconnected'))
    await store.reloadTab(store.activeTab!.id)
    await nextTick()
    expect(wrapper.find('[data-testid="editor-error"]').text()).toContain('Runner disconnected')
    await buttons(wrapper, 'Retry').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="editor-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="monaco-stub"]').exists()).toBe(true)
  })

  it('routes buffer edits, save shortcuts and diff actions to the shared store', async () => {
    const store = useEditorStore()
    await store.openDiff(WS, '/workspace/repo', 'a.ts')
    const wrapper = mount(WorkspaceEditor)
    await flushPromises()
    const monaco = wrapper.findComponent({ name: 'MonacoEditor' })
    expect(monaco.props()).toMatchObject({
      diff: true,
      staged: false,
      readOnly: false,
      gitActionsEnabled: true,
      original: 'before\n',
      modelValue: 'after\n',
    })
    monaco.vm.$emit('update:modelValue', 'edited\n')
    await nextTick()
    expect(monaco.props('gitActionsEnabled')).toBe(false)
    const event = new KeyboardEvent('keydown', { key: 's', ctrlKey: true, cancelable: true })
    document.dispatchEvent(event)
    await flushPromises()
    expect(event.defaultPrevented).toBe(true)
    expect(saveEditorFile).toHaveBeenCalledWith(WS, FILE, 'edited\n', 'worktree-revision')
    const apply = vi.spyOn(store, 'applyChange').mockResolvedValue(true)
    const range = { originalStart: 1, originalEnd: 2, modifiedStart: 1, modifiedEnd: 2 }
    monaco.vm.$emit('stage', range)
    await flushPromises()
    expect(apply).toHaveBeenLastCalledWith(store.activeTab!.id, 'stage', range)
    monaco.vm.$emit('discard', range)
    await flushPromises()
    expect(apply).toHaveBeenLastCalledWith(store.activeTab!.id, 'revert', range)
  })

  it('requires confirmation before reloading a dirty draft', async () => {
    const store = useEditorStore()
    await store.openFile(WS, FILE)
    store.updateContent(store.activeTab!.id, 'draft\n')
    const wrapper = mount(frame)
    await flushPromises()
    await wrapper.find('[aria-label="Reload from disk"]').trigger('click')
    expect(readEditorFile).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="editor-unsaved-dialog"]').text()).toContain(
      'Discard changes and reload?',
    )
    await buttons(wrapper, 'Discard and reload').trigger('click')
    await flushPromises()
    expect(store.activeTab).toMatchObject({ content: 'before\n', dirty: false })
    expect(wrapper.find('[data-testid="editor-unsaved-dialog"]').exists()).toBe(false)
  })

  it.each(['image.png', 'document.pdf', 'binary.dat'])(
    'keeps %s previews read-only outside Monaco',
    async (filename) => {
      if (filename.endsWith('.dat')) {
        vi.mocked(readEditorFile).mockRejectedValueOnce(
          new ApiRequestError(422, 'Binary', 'binary_file'),
        )
        vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
          content: btoa('\0binary'),
          size: 7,
          truncated: false,
        } as never)
      }
      const store = useEditorStore()
      await store.openFile(WS, `/workspace/${filename}`)
      const wrapper = mount(frame)
      await flushPromises()
      expect(wrapper.find('[data-testid="monaco-stub"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="editor-save"]').attributes('disabled')).toBeDefined()
      if (filename.endsWith('.png'))
        expect(wrapper.find('img').attributes('src')).toBe('data:image/png;base64,cHJldmlldw==')
      else if (filename.endsWith('.pdf'))
        expect(wrapper.find('iframe').attributes('title')).toBe('PDF preview')
      else expect(wrapper.find('[data-testid="editor-binary"]').exists()).toBe(true)
    },
  )
})
