import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, onMounted, onUnmounted, ref } from 'vue'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import WorkspaceToolsSplit from './WorkspaceToolsSplit.vue'
import { useEditorStore } from '@/stores/editor'
import { useSidePanelStore } from '@/stores/sidePanel'
import { readEditorFile, saveEditorFile } from '@/services/editor.api'

enableAutoUnmount(afterEach)

const mocks = vi.hoisted(() => ({
  download: vi.fn(),
  reset: vi.fn(),
  refresh: vi.fn(),
  chatMounts: 0,
  chatUnmounts: 0,
}))
vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(),
  saveEditorFile: vi.fn(),
  readGitFileContent: vi.fn(),
}))
vi.mock('@/services/git.api', () => ({ runGitOperation: vi.fn() }))
vi.mock('@/services/socket', () => ({ readWorkspaceFile: vi.fn() }))
vi.mock('@/stores/fileExplorer', () => ({
  useFileExplorerStore: () => ({
    reset: mocks.reset,
    downloadFile: mocks.download,
    formatBytes: String,
  }),
}))
vi.mock('@/stores/git', () => ({
  useGitStore: () => ({
    reset: mocks.reset,
    refresh: mocks.refresh,
    $onAction: vi.fn(() => vi.fn()),
  }),
}))
vi.mock('@/stores/desktop', () => ({
  useDesktopStore: () => ({ reset: mocks.reset, close: vi.fn() }),
}))
vi.mock('@/stores/terminal', () => ({
  useTerminalStore: () => ({ reset: mocks.reset, close: vi.fn(), open: vi.fn() }),
}))
vi.mock('@/stores/workspaceImages', () => ({
  useWorkspaceImageStore: () => ({ reset: mocks.reset }),
}))
vi.mock('@/composables/useWorkspaceFileEvents', () => ({ useWorkspaceFileEvents: vi.fn() }))
vi.mock('@/components/ui/sidebar', () => ({
  SidebarTrigger: { template: '<button aria-label="Toggle sidebar" />' },
}))
vi.mock('@/components/files/WorkspaceFileIcon.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/editor/MonacoEditor.vue', () => ({
  __esModule: true,
  default: { template: '<div data-testid="monaco-stub" />' },
}))
vi.mock('./DesktopSurface.vue', () => ({ default: { template: '<div />' } }))
vi.mock('./WorkspaceDesktop.vue', () => ({ default: { template: '<div />' } }))

const Chat = defineComponent({
  setup() {
    const draft = ref('')
    onMounted(() => {
      mocks.chatMounts += 1
    })
    onUnmounted(() => {
      mocks.chatUnmounts += 1
    })
    return { draft }
  },
  template: '<textarea aria-label="Chat draft" v-model="draft" />',
})

async function settle(): Promise<void> {
  await vi.dynamicImportSettled()
  await flushPromises()
}

function mountSplit() {
  return mount(WorkspaceToolsSplit, {
    attachTo: document.body,
    props: { workspaceId: 'ws-1', canPrompt: true },
    slots: {
      header: '<div data-testid="chat-header">Workspace header</div>',
      default: Chat,
    },
    global: {
      stubs: {
        GitPanel: { template: '<div data-testid="git-tool" />' },
        FileExplorerPanel: { template: '<div data-testid="files-tool" />' },
        WorkspaceTerminal: { template: '<div data-testid="terminal-tool" />' },
        SidePanelDesktop: { template: '<div data-testid="desktop-tool" />' },
      },
    },
  })
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mocks.chatMounts = 0
  mocks.chatUnmounts = 0
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('1024'),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
  vi.mocked(readEditorFile).mockImplementation(async (_ws, path) => ({
    path,
    content: 'file\n',
    revision: 'version-1',
  }))
  vi.mocked(saveEditorFile).mockImplementation(async (_ws, path, content) => ({
    path,
    content,
    revision: 'version-2',
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('editor and sidebar chat layout', () => {
  it('moves the same chat into the sidebar while retaining the editor, drafts and tool state', async () => {
    const sidePanel = useSidePanelStore()
    sidePanel.open('files')
    const editor = useEditorStore()
    const wrapper = mountSplit()
    await settle()
    const chat = wrapper.get('textarea')
    await chat.setValue('An unsent chat draft')
    const filesTool = wrapper.get('[data-testid="files-tool"]').element
    await editor.openFile('ws-1', '/workspace/a.ts')
    const tabId = editor.activeTab!.id
    editor.updateContent(tabId, 'Unsaved code')
    await settle()
    await wrapper.get('[aria-label="Open chat"]').trigger('click')
    await settle()

    const target = wrapper.get('[data-testid="workspace-sidebar-chat-target"]')
    expect(target.find('textarea').element).toBe(chat.element)
    expect((chat.element as HTMLTextAreaElement).value).toBe('An unsent chat draft')
    expect(editor.activeTab).toMatchObject({ id: tabId, content: 'Unsaved code', dirty: true })
    expect(wrapper.get('[data-testid="workspace-editor"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="workspace-side-panel-tools"]').isVisible()).toBe(false)
    expect(sidePanel.activeTab).toBe('files')
    expect(sidePanel.isOpen).toBe(true)
    expect(mocks.chatMounts).toBe(1)
    expect(mocks.chatUnmounts).toBe(0)

    await wrapper.get('[data-testid="sidebar-chat-close"]').trigger('click')
    await settle()
    expect(wrapper.get('[data-testid="workspace-side-panel-tools"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="files-tool"]').element).toBe(filesTool)
    expect(sidePanel.isOpen).toBe(true)
    expect(editor.isOpen).toBe(true)
    expect(mocks.chatMounts).toBe(1)
    expect(mocks.chatUnmounts).toBe(0)
  })

  it('opens chat from a closed sidebar without starting hidden tools or persisting chat as a tool tab', async () => {
    const sidePanel = useSidePanelStore()
    const editor = useEditorStore()
    const wrapper = mountSplit()
    await editor.openFile('ws-1', '/workspace/a.ts')
    await settle()
    await wrapper.get('[aria-label="Open chat"]').trigger('click')
    await settle()
    expect(wrapper.get('[data-testid="workspace-sidebar-chat"]').isVisible()).toBe(true)
    expect(wrapper.find('[data-testid="terminal-tool"]').exists()).toBe(false)
    expect(sidePanel.isOpen).toBe(false)
    expect(sidePanel.hasOpened).toBe(false)

    await wrapper.get('[aria-label="Close chat"]').trigger('click')
    await settle()
    expect(wrapper.find('[data-testid="workspace-side-panel"]').exists()).toBe(false)
    expect(wrapper.findAll('textarea')).toHaveLength(1)
    expect(mocks.chatMounts).toBe(1)
    expect(mocks.chatUnmounts).toBe(0)
    expect(editor.tabs).toHaveLength(1)
  })

  it('returns chat to the main area after the last file closes and does not dock it on reopening', async () => {
    const editor = useEditorStore()
    const wrapper = mountSplit()
    await editor.openFile('ws-1', '/workspace/a.ts')
    await settle()
    await wrapper.get('[aria-label="Open chat"]').trigger('click')
    await settle()
    await wrapper.get('textarea').setValue('Retain this draft')
    editor.closeTab(editor.activeTab!.id)
    await settle()
    expect(wrapper.get('[data-testid="chat-header"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="workspace-chat-content"]').isVisible()).toBe(true)
    expect(wrapper.find('[data-testid="workspace-sidebar-chat"]').exists()).toBe(false)
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('Retain this draft')
    expect(mocks.chatMounts).toBe(1)
    expect(mocks.chatUnmounts).toBe(0)
    await editor.openFile('ws-1', '/workspace/b.ts')
    await settle()
    expect(wrapper.find('[aria-label="Open chat"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="workspace-sidebar-chat"]').exists()).toBe(false)
  })

  it.each(['workspace', 'unavailable'] as const)(
    'clears docked chat on %s changes',
    async (change) => {
      const editor = useEditorStore()
      const wrapper = mountSplit()
      await editor.openFile('ws-1', '/workspace/a.ts')
      await settle()
      await wrapper.get('[aria-label="Open chat"]').trigger('click')
      await settle()
      await wrapper.setProps(
        change === 'workspace' ? { workspaceId: 'ws-2' } : { canPrompt: false },
      )
      await settle()
      expect(wrapper.get('[data-testid="workspace-chat-content"]').isVisible()).toBe(true)
      expect(wrapper.find('[data-testid="workspace-sidebar-chat"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="editor-tabs"]').exists()).toBe(false)
    },
  )

  it('routes the single-row file actions to the editor commands', async () => {
    const editor = useEditorStore()
    const wrapper = mountSplit()
    await editor.openFile('ws-1', '/workspace/a.ts')
    await settle()
    expect(
      wrapper
        .get('[data-testid="workspace-editor"]')
        .find('button[aria-label="Download file"]')
        .exists(),
    ).toBe(false)
    editor.updateContent(editor.activeTab!.id, 'Saved code')
    await settle()
    const tabs = wrapper.get('[data-testid="editor-tabs"]')
    await tabs.get('[aria-label="Save file"]').trigger('click')
    await settle()
    expect(saveEditorFile).toHaveBeenCalledWith(
      'ws-1',
      '/workspace/a.ts',
      'Saved code',
      'version-1',
    )
    await tabs.get('[aria-label="Download file"]').trigger('click')
    expect(mocks.download).toHaveBeenCalledWith('ws-1', '/workspace/a.ts')
    await tabs.get('[aria-label="Reload from disk"]').trigger('click')
    await settle()
    expect(readEditorFile).toHaveBeenCalledTimes(2)
  })
})
