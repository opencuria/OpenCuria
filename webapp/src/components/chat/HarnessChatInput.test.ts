import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessChatInput from './HarnessChatInput.vue'
import ComposerRichEditor from './ComposerRichEditor.vue'
import type { VueWrapper } from '@vue/test-utils'
import { OPEN_SETTINGS_EVENT } from '@/components/settings/settingsTabs'
import * as harnessApi from '@/services/harness.api'
import type { ProviderModel } from '@/lib/harnessModels'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'
import { resetRecentModelsCache } from '@/lib/recentModels'
import { resetAgentConfigsCache } from '@/lib/agentConfigs'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { sendFilesUpload } from '@/services/socket'
import { CHAT_UPLOAD_DIR } from '@/lib/chatUpload'

vi.mock('@/services/socket', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/socket')>('@/services/socket')
  return {
    ...actual,
    sendFilesUpload: vi.fn(),
  }
})

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listProviderModels: vi.fn(),
    listAgentConfigs: vi.fn(),
    listRecentModels: vi.fn().mockResolvedValue([]),
    saveRecentModel: vi.fn(),
  }
})

const listProviderModelsMock = vi.mocked(harnessApi.listProviderModels)
const listAgentConfigsMock = vi.mocked(harnessApi.listAgentConfigs)

const catalog: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 200_000,
    max_output_tokens: 32_768,
  },
  {
    id: 'openrouter/model-small',
    name: 'Small',
    provider: 'openrouter',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
]

const dropdownStubs = {
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: { template: '<button type="button"><slot /></button>' },
  DropdownMenuSub: { template: '<div><slot /></div>' },
  DropdownMenuSubTrigger: { template: '<div><slot /></div>' },
  DropdownMenuSubContent: { template: '<div><slot /></div>' },
  DropdownMenuSeparator: { template: '<hr />' },
}

function mountInput(props: Record<string, unknown> = {}, attachTo?: Element) {
  return mount(HarnessChatInput, {
    props: { workspaceId: 'ws-1', ...props },
    ...(attachTo ? { attachTo } : {}),
    global: { stubs: dropdownStubs },
  })
}

/** Drive the contenteditable exactly as a plain-text browser input. */
async function setEditorText(wrapper: VueWrapper, text: string): Promise<void> {
  const el = wrapper.get('[data-testid="composer-textarea"]').element as HTMLElement
  el.textContent = text
  const range = document.createRange()
  range.selectNodeContents(el)
  range.collapse(false)
  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
  await wrapper.get('[data-testid="composer-textarea"]').trigger('input')
}
function editorText(wrapper: VueWrapper): string {
  return (wrapper.findComponent(ComposerRichEditor).vm as unknown as { value(): string }).value()
}
function editorCursor(wrapper: VueWrapper, offset: number): void {
  const editor = wrapper.findComponent(ComposerRichEditor).vm as unknown as { setCursor(offset: number): void }
  editor.setCursor(offset)
}

describe('HarnessChatInput', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    resetProviderCatalogCache()
    resetAgentConfigsCache()
    resetRecentModelsCache()
    listAgentConfigsMock.mockResolvedValue([
      { agent: 'build', mode: 'primary', description: '', model: 'openrouter/model-big', effort: 'high', inherit_model: false, effort_strategy: 'fixed' },
      { agent: 'plan', mode: 'primary', description: '', model: 'openrouter/model-small', effort: '', inherit_model: false, effort_strategy: 'fixed' },
    ])
    listProviderModelsMock.mockResolvedValue(catalog)
  })

  it('loads the provider catalog into the model picker', async () => {
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(listProviderModelsMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    const html = wrapper.html()
    expect(listProviderModelsMock).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Skills')
    expect(wrapper.text()).not.toContain('Fast')
  })

  it('renders mode pill, context ring, paperclip, and send arrow', async () => {
    const wrapper = mountInput()
    expect(wrapper.find('[data-testid="composer-mode-trigger"]').exists()).toBe(true)
    const ring = wrapper.find('[data-testid="composer-context-usage"]')
    const attach = wrapper.find('[data-testid="composer-attach"]')
    expect(ring.exists()).toBe(true)
    expect(attach.exists()).toBe(true)
    expect(ring.element.compareDocumentPosition(attach.element)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
    expect(wrapper.find('[data-testid="composer-send"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-textarea"]').attributes('data-placeholder')).toContain(
      '/ for skills',
    )
  })

  it('keeps all composer controls inside a responsive wrapping toolbar', () => {
    const wrapper = mountInput()
    const toolbar = wrapper.find('[data-testid="composer-toolbar"]')
    const leading = wrapper.find('[data-testid="composer-toolbar-leading"]')

    expect(toolbar.classes()).toContain('flex-wrap')
    expect(leading.classes()).toContain('min-w-0')
    expect(leading.classes()).toContain('flex-wrap')
    expect(wrapper.find('[data-testid="composer-send"]').exists()).toBe(true)
  })

  it('starts as a single line and caps growth at 200px', async () => {
    const wrapper = mountInput()
    const textarea = wrapper.find('[data-testid="composer-textarea"]')
    expect(textarea.classes()).toContain('min-h-10')
    expect(textarea.classes()).toContain('md:min-h-9')
    expect(textarea.classes()).toContain('max-h-[200px]')
  })

  it('clamps autosize height at 200px when content overflows', async () => {
    const wrapper = mountInput()
    const el = wrapper.get('[data-testid="composer-textarea"]').element as HTMLElement
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 500 })
    await setEditorText(wrapper, 'line\n'.repeat(20))
    await wrapper.vm.$nextTick()
    expect(el.style.height).toBe('200px')
    expect(el.style.overflowY).toBe('auto')
  })

  it('resets to one line after send', async () => {
    const wrapper = mountInput()
    const el = wrapper.get('[data-testid="composer-textarea"]').element as HTMLElement
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 80 })
    await setEditorText(wrapper, 'hello world')
    await wrapper.vm.$nextTick()
    expect(el.style.height).toBe('80px')
    await wrapper.get('[data-testid="composer-textarea"]').trigger('keydown', { key: 'Enter' })
    await wrapper.vm.$nextTick()
    expect(el.style.height).toBe('')
    expect(el.style.overflowY).toBe('')
  })

  it('renders inline badges and removes only the selected mention via its icon X', async () => {
    const wrapper = mountInput()
    const vm = wrapper.vm as unknown as { setPrompt(text: string): void }
    vm.setPrompt('Review @file:/workspace/src/shell.py with @agent:plan please')
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="composer-file-badge"]').text()).toContain('shell.py')
    expect(wrapper.get('[data-testid="composer-file-badge"]').attributes('title')).toBe('/workspace/src/shell.py')
    expect(wrapper.get('[data-testid="composer-agent-badge"]').text()).toContain('plan')
    expect(editorText(wrapper)).toBe('Review @file:/workspace/src/shell.py with @agent:plan please')
    await wrapper.get('[data-testid="composer-file-badge"] [data-testid="composer-badge-remove"]').trigger('click')
    expect(editorText(wrapper)).toBe('Review with @agent:plan please')
    expect(wrapper.find('[data-testid="composer-file-badge"]').exists()).toBe(false)
    await wrapper.get('[data-testid="composer-textarea"]').trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('send')?.[0]?.[0]).toBe('Review with @agent:plan please')
  })

  it('keeps Shift+Enter as a single newline when editing before a badge', async () => {
    const wrapper = mountInput({}, document.body)
    const vm = wrapper.vm as unknown as { setPrompt(text: string): void }
    vm.setPrompt('before @file:/workspace/a.py after')
    await wrapper.vm.$nextTick()
    ;(wrapper.get('[data-testid="composer-textarea"]').element as HTMLElement).focus()
    editorCursor(wrapper, 0)
    await wrapper.get('[data-testid="composer-textarea"]').trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(editorText(wrapper)).toBe('\nbefore @file:/workspace/a.py after')
    wrapper.unmount()
    expect(wrapper.get('[data-testid="composer-file-badge"]').text()).toContain('a.py')
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('shows image thumbnail above inline badge and removes both with thumbnail X', async () => {
    const wrapper = mountInput()
    const vm = wrapper.vm as unknown as { setPrompt(text: string): void }
    vm.setPrompt('See @file:/workspace/a.png now')
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="composer-file-badge"]').text()).toContain('a.png')
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="mention-images"] .relative').classes()).toContain('w-20')
    await wrapper.get('[data-testid="mention-image-remove"]').trigger('click')
    expect(editorText(wrapper)).toBe('See now')
    expect(wrapper.find('[data-testid="composer-file-badge"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
  })

  it('leaves unsafe or incomplete tokens as editable plain text', async () => {
    const wrapper = mountInput()
    await setEditorText(wrapper, 'See @file:/workspace/../secret.png and @file:')
    expect(wrapper.find('[data-testid="composer-file-badge"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
  })

  it('shows settings CTA when the model catalog has no available models', async () => {
    listProviderModelsMock.mockResolvedValue([])
    const wrapper = mountInput()
    await vi.waitFor(() => expect(wrapper.find('[data-testid="composer-provider-cta"]').exists()).toBe(true))
    const events: Array<{ tab?: string }> = []
    const listener = (e: Event) => events.push((e as CustomEvent<{ tab?: string }>).detail ?? {})
    window.addEventListener(OPEN_SETTINGS_EVENT, listener)
    try {
      await wrapper.get('[data-testid="composer-provider-cta"]').trigger('click')
      expect(events).toEqual([{ tab: 'provider' }])
    } finally {
      window.removeEventListener(OPEN_SETTINGS_EVENT, listener)
    }
  })

  it('loads models and agent defaults without fetching provider settings', async () => {
    const wrapper = mountInput()
    await vi.waitFor(() => expect(listProviderModelsMock).toHaveBeenCalled())
    expect(listProviderModelsMock).toHaveBeenCalled()
    expect(listAgentConfigsMock).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('toggles plan/build with Shift+Tab', async () => {
    const wrapper = mountInput({ mode: 'build' })
    await wrapper.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(wrapper.emitted('update:mode')?.[0]).toEqual(['plan'])
    await wrapper.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(wrapper.emitted('update:mode')?.[1]).toEqual(['build'])
  })

  it('sends prompt, mode, model, skill ids, and effort', async () => {
    const wrapper = mountInput({ mode: 'plan' })
    const textarea = wrapper.get('[data-testid="composer-textarea"]')
    await setEditorText(wrapper, 'hello world')
    await textarea.trigger('keydown', { key: 'Enter' })
    const sends = wrapper.emitted('send') ?? []
    expect(sends.length).toBeGreaterThan(0)
    expect(sends[0]![0]).toBe('hello world')
    expect(sends[0]![1]).toBe('plan')
    expect(typeof sends[0]![2]).toBe('string')
    expect(sends[0]![3]).toEqual([])
    expect(typeof sends[0]![4]).toBe('string')
  })

  it('attaches a skill from the slash picker and includes it on send', async () => {
    const wrapper = mountInput({
      skillOptions: [
        {
          id: 'skill-1',
          name: 'Lint rules',
          body: 'Always lint',
          scope: 'personal',
          created_by_email: null,
          created_at: '2026-03-29T10:00:00.000Z',
          updated_at: '2026-03-29T10:00:00.000Z',
        },
      ],
    })
    await vi.waitFor(() => {
      expect(listProviderModelsMock).toHaveBeenCalled()
    })

    const textarea = wrapper.get('[data-testid="composer-textarea"]')
    await setEditorText(wrapper, '/Lin')
    editorCursor(wrapper, 4)
    await textarea.trigger('input')
    expect(wrapper.find('[role="listbox"]').text()).toContain('Lint rules')
    await textarea.trigger('keydown', { key: 'Enter' })
    expect(wrapper.text()).toContain('Lint rules')

    await setEditorText(wrapper, 'use skills')
    await textarea.trigger('keydown', { key: 'Enter' })
    const sends = wrapper.emitted('send') ?? []
    expect(sends.length).toBeGreaterThan(0)
    expect(sends[0]![3]).toEqual(['skill-1'])
  })

  it('mirrors mention state to the parent sheet stack in controlled mode', async () => {
    const wrapper = mountInput({
      mentionControlled: true,
      files: [{ name: 'a.ts', path: '/workspace/a.ts', type: 'file', size: 1 }],
    })
    const textarea = wrapper.get('[data-testid="composer-textarea"]')
    await setEditorText(wrapper, '@a')
    editorCursor(wrapper, 2)
    await textarea.trigger('input')

    const changes = wrapper.emitted('mention-change') ?? []
    expect(changes.length).toBeGreaterThan(0)
    const last = changes[changes.length - 1]!
    expect(last[0]).toBe(true)
    expect(Array.isArray(last[2])).toBe(true)
    expect((last[2] as Array<{ label: string }>).length).toBeGreaterThan(0)
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })

  it('emits mention-select instead of inserting in controlled mode', async () => {
    const wrapper = mountInput({
      mentionControlled: true,
      files: [{ name: 'a.ts', path: '/workspace/a.ts', type: 'file', size: 1 }],
    })
    const textarea = wrapper.get('[data-testid="composer-textarea"]')
    await setEditorText(wrapper, '@a')
    editorCursor(wrapper, 2)
    await textarea.trigger('input')
    await textarea.trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('mention-select')?.length).toBeGreaterThan(0)
    expect((wrapper.emitted('send') ?? []).length).toBe(0)
  })

  it('fetches workspace files when @ is typed', async () => {
    const store = useFileExplorerStore()
    const findFiles = vi.spyOn(store, 'findFiles').mockResolvedValue(['/workspace/src/a.ts'])
    const wrapper = mountInput()
    const textarea = wrapper.get('[data-testid="composer-textarea"]')
    await setEditorText(wrapper, '@src')
    editorCursor(wrapper, 4)
    await textarea.trigger('input')
    expect(findFiles).toHaveBeenCalledWith('ws-1', 'src', 50)
  })

  it('moves mention selection with arrow keys', async () => {
    const store = useFileExplorerStore()
    vi.spyOn(store, 'findFiles').mockResolvedValue([])
    const wrapper = mountInput({
      files: [
        { name: 'a.ts', path: '/workspace/a.ts', type: 'file', size: 1 },
        { name: 'b.ts', path: '/workspace/b.ts', type: 'file', size: 1 },
      ],
    })
    const textarea = wrapper.get('[data-testid="composer-textarea"]')
    await setEditorText(wrapper, '@')
    editorCursor(wrapper, 1)
    await textarea.trigger('input')
    const options = wrapper.findAll('[role="option"]')
    expect(options.length).toBeGreaterThan(1)
    expect(options[0]!.attributes('aria-selected')).toBe('true')
    await options[0]!.trigger('mousemove', { clientX: 8, clientY: 8 })
    await textarea.trigger('keydown', { key: 'ArrowDown' })
    expect(wrapper.findAll('[role="option"]')[1]!.attributes('aria-selected')).toBe('true')
    await options[0]!.trigger('mousemove', { clientX: 8, clientY: 8 })
    expect(wrapper.findAll('[role="option"]')[1]!.attributes('aria-selected')).toBe('true')
    await options[0]!.trigger('mousemove', { clientX: 14, clientY: 8 })
    expect(wrapper.findAll('[role="option"]')[0]!.attributes('aria-selected')).toBe('true')
  })

  it('emits toggle-context when the usage ring is clicked', async () => {
    const wrapper = mountInput({ contextUsed: 12_000 })
    await wrapper.find('[data-testid="composer-context-usage"]').trigger('click')
    expect(wrapper.emitted('toggle-context')).toEqual([[]])
  })

  it('fills the context ring from catalog limit and used tokens', async () => {
    resetProviderCatalogCache()
    const wrapper = mountInput({ contextUsed: 50_000, model: 'openrouter/model-big' })
    await vi.waitFor(() => expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).not.toContain('Loading'))
    const rings = wrapper.findAll('[data-testid="composer-context-usage"] circle')
    expect(rings).toHaveLength(2)
    const circumference = 2 * Math.PI * 6
    const progress = rings[1]!
    expect(progress.attributes('stroke-dashoffset')).toBe(String(circumference * 0.75))
    expect(wrapper.find('[data-testid="composer-context-usage"]').attributes('aria-label')).toBe(
      'Context usage 25%',
    )
  })

  it('emits context metrics when used tokens or catalog limit change', async () => {
    resetProviderCatalogCache()
    const wrapper = mountInput({ contextUsed: 50_000, model: 'openrouter/model-big' })
    await vi.waitFor(() => expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).not.toContain('Loading'))
    const metrics = wrapper.emitted('context-metrics') ?? []
    expect(metrics.length).toBeGreaterThan(0)
    const last = metrics[metrics.length - 1]![0] as {
      used: number
      limit: number
      percent: number
    }
    expect(last.used).toBe(50_000)
    expect(last.limit).toBe(200_000)
    expect(last.percent).toBe(25)
  })

  it('keeps rounded corners and an accent focus ring when a sheet is attached', () => {
    const wrapper = mountInput({ attached: true })
    const card = wrapper.find('[data-testid="composer-card"]')
    expect(card.exists()).toBe(true)
    expect(card.classes()).toContain('rounded-3xl')
    expect(card.classes()).toContain('focus-within:border-primary')
    expect(card.classes()).toContain('focus-within:shadow-md')
    expect(card.classes()).not.toContain('rounded-t-none')
    expect(card.classes()).not.toContain('border-t-0')
  })

  it('styles the stop button with primary accent colors when stoppable', () => {
    const wrapper = mountInput({ stoppable: true })
    const stop = wrapper.find('[data-testid="composer-stop"]')
    expect(stop.exists()).toBe(true)
    expect(stop.classes()).toContain('bg-primary')
    expect(stop.classes()).toContain('text-primary-foreground')
    expect(stop.classes()).not.toContain('bg-destructive')
  })

  it('opens the native file dialog when the paperclip is clicked', async () => {
    const wrapper = mountInput()
    const input = wrapper.find('[data-testid="composer-file-input"]')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).multiple).toBe(true)
    const clickSpy = vi.spyOn(input.element as HTMLInputElement, 'click').mockImplementation(() => {})
    await wrapper.find('[data-testid="composer-attach"]').trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    clickSpy.mockRestore()
  })

  it('disables the paperclip when the workspace is not ready', () => {
    const wrapper = mountInput({ disabled: true })
    const attach = wrapper.find('[data-testid="composer-attach"]')
    expect(attach.attributes('disabled')).toBeDefined()
  })
})

/** Build a FileList-like with real File objects (jsdom has File but no DataTransfer). */
function makeFileList(files: File[]): FileList {
  const list = {
    length: files.length,
    item: (index: number) => files[index] ?? null,
  } as unknown as FileList & { [index: number]: File }
  for (let i = 0; i < files.length; i++) {
    list[i] = files[i]!
  }
  return list as FileList
}

describe('HarnessChatInput chat upload', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    sessionStorage.clear()
    vi.clearAllMocks()
    vi.useRealTimers()
    resetProviderCatalogCache()
    resetAgentConfigsCache()
    resetRecentModelsCache()
    listAgentConfigsMock.mockResolvedValue([
      { agent: 'build', mode: 'primary', description: '', model: 'openrouter/model-big', effort: 'high', inherit_model: false, effort_strategy: 'fixed' },
      { agent: 'plan', mode: 'primary', description: '', model: 'openrouter/model-small', effort: '', inherit_model: false, effort_strategy: 'fixed' },
    ])
    listProviderModelsMock.mockResolvedValue(catalog)
  })

  /** Resolve the tracked upload the way a backend `files:upload_result` would. */
  function succeedUpload(requestId: string, path = CHAT_UPLOAD_DIR): void {
    useFileExplorerStore().handleUploadResult(requestId, path, 'success', 'ws-1')
  }

  function uploadRequest(index = 0) {
    const calls = vi.mocked(sendFilesUpload).mock.calls
    return {
      requestId: calls[index]![1] as string,
      path: calls[index]![2] as string,
      filename: calls[index]![3] as string,
    }
  }

  it('uploads a picked file and inserts an @file: token', async () => {
    const store = useFileExplorerStore()
    vi.spyOn(store, 'fetchDirectory').mockResolvedValue(undefined)
    const file = new File(['hello'], 'my notes.txt', { type: 'text/plain' })
    resetProviderCatalogCache()
    const wrapper = mountInput()

    const input = wrapper.find('[data-testid="composer-file-input"]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: makeFileList([file]),
    })
    await input.trigger('change')
    await vi.waitFor(() => {
      expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(1)
    })
    await vi.waitFor(() => expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).not.toContain('Loading'))

    const { requestId, path, filename } = uploadRequest()
    expect(path).toBe(CHAT_UPLOAD_DIR)
    expect(filename).toBe('my_notes.txt')
    succeedUpload(requestId)
    await vi.waitFor(() => {
      expect(editorText(wrapper)).toContain(
        `@file:${CHAT_UPLOAD_DIR}/my_notes.txt `,
      )
    })
  })

  it('deduplicates against existing upload-dir files instead of overwriting', async () => {
    const store = useFileExplorerStore()
    store.setTree('/workspace', [
      { name: '.opencuria', path: '/workspace/.opencuria', type: 'directory', size: 0 },
    ])
    store.setTree('/workspace/.opencuria', [
      { name: 'user-uploaded', path: CHAT_UPLOAD_DIR, type: 'directory', size: 0 },
    ])
    store.setTree(CHAT_UPLOAD_DIR, [
      { name: 'a.txt', path: `${CHAT_UPLOAD_DIR}/a.txt`, type: 'file', size: 1 },
    ])
    const fetchSpy = vi.spyOn(store, 'fetchDirectory').mockResolvedValue(undefined)
    const wrapper = mountInput()

    const input = wrapper.find('[data-testid="composer-file-input"]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: makeFileList([new File(['x'], 'a.txt', { type: 'text/plain' })]),
    })
    await input.trigger('change')
    await vi.waitFor(() => {
      expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(1)
    })

    const { requestId, filename } = uploadRequest()
    expect(filename).toBe('a_1.txt')
    expect(fetchSpy).not.toHaveBeenCalledWith('ws-1', CHAT_UPLOAD_DIR)
    succeedUpload(requestId)
    await vi.waitFor(() => {
      expect(editorText(wrapper)).toContain(`@file:${CHAT_UPLOAD_DIR}/a_1.txt `)
    })
  })

  it('uploads dropped files on the composer card', async () => {    const store = useFileExplorerStore()
    vi.spyOn(store, 'fetchDirectory').mockResolvedValue(undefined)
    const wrapper = mountInput()
    const card = wrapper.find('[data-testid="composer-card"]')

    const file = new File(['hi'], 'drop.txt', { type: 'text/plain' })
    const event = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: makeFileList([file]), types: ['Files'] },
    })
    card.element.dispatchEvent(event)
    await vi.waitFor(() => {
      expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(1)
    })

    const { requestId } = uploadRequest()
    expect(vi.mocked(sendFilesUpload).mock.calls[0]![2]).toBe(CHAT_UPLOAD_DIR)
    succeedUpload(requestId)
    await vi.waitFor(() => {
      expect(editorText(wrapper)).toContain(
        `@file:${CHAT_UPLOAD_DIR}/drop.txt `,
      )
    })
  })

  it('uploads a doubly-handled drop only once (composer card + parent zone)', async () => {
    const store = useFileExplorerStore()
    vi.spyOn(store, 'fetchDirectory').mockResolvedValue(undefined)
    // Keep the tracked upload pending so the second batch runs strictly
    // while the first is still in flight (reproduces the double drop).
    const pending: Array<(data: unknown) => void> = []
    const trackSpy = vi.spyOn(store, 'trackAndUpload').mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          const wrapped = (data: unknown) => {
            const ok =
              data !== null && typeof data === 'object' && (data as { ok?: boolean }).ok === true
            if (ok) resolve()
          }
          pending.push(wrapped)
        }),
    )
    const wrapper = mountInput()
    const vm = wrapper.vm as unknown as {
      uploadChatFiles: (files: File[] | FileList) => Promise<void>
    }

    const file = new File(['hi'], 'drop.txt', { type: 'text/plain' })
    const files = makeFileList([file])
    // Simulate the drop bubbling: the composer card handler runs first,
    // then — before it finishes — the parent panel/home forwarder calls
    // `uploadChatFiles` again with the same files.
    const first = vm.uploadChatFiles(files)
    await vi.waitFor(() => {
      expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(1)
    })
    const second = vm.uploadChatFiles(files)
    // Let the serialized second batch start before resolving the upload.
    await new Promise((resolve) => setTimeout(resolve, 20))
    pending.forEach((done) => done({ ok: true }))
    await Promise.all([first, second])
    // The second batch was serialized behind the first and skips the
    // already-tracked upload, so the file is sent exactly once.
    expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(1)
    expect(trackSpy).toHaveBeenCalledTimes(1)

    await vi.waitFor(() => {
      expect(editorText(wrapper)).toContain(
        `@file:${CHAT_UPLOAD_DIR}/drop.txt `,
      )
    })
    const value = editorText(wrapper) as string
    expect(value.match(/@file:/g)).toHaveLength(1)
  })

  it('ignores drops while the workspace is not ready', async () => {
    const wrapper = mountInput({ disabled: true })
    const card = wrapper.find('[data-testid="composer-card"]')
    const file = new File(['hi'], 'drop.txt', { type: 'text/plain' })
    const event = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: makeFileList([file]), types: ['Files'] },
    })
    card.element.dispatchEvent(event)
    await wrapper.vm.$nextTick()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(vi.mocked(sendFilesUpload)).not.toHaveBeenCalled()
    expect(editorText(wrapper)).toBe('')
  })

  it('skips >10 MiB files before arrayBuffer() with a visible toast and no send', async () => {
    const store = useFileExplorerStore()
    vi.spyOn(store, 'fetchDirectory').mockResolvedValue(undefined)
    const wrapper = mountInput()
    const big = new File(['x'], 'big.bin', { type: 'application/octet-stream' })
    Object.defineProperty(big, 'size', { value: 10 * 1024 * 1024 + 1 })
    const arrayBufferSpy = vi.spyOn(big, 'arrayBuffer')

    const input = wrapper.find('[data-testid="composer-file-input"]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: makeFileList([big]),
    })
    await input.trigger('change')
    await vi.waitFor(() => {
      expect(arrayBufferSpy).not.toHaveBeenCalled()
    })
    // The size guard runs before any tracked upload; the only send would be
    // the best-effort upload-dir prefetch (mocked), never an upload itself.
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(vi.mocked(sendFilesUpload)).not.toHaveBeenCalled()
    expect(editorText(wrapper)).toBe('')
    const { toast } = await import('vue-sonner')
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Upload failed',
      expect.objectContaining({ description: expect.stringContaining('10 MB') }),
    )
  })

  it('a synchronous sendFilesUpload throw fails visibly without inserting a token', async () => {
    const store = useFileExplorerStore()
    vi.spyOn(store, 'fetchDirectory').mockResolvedValue(undefined)
    vi.mocked(sendFilesUpload).mockImplementationOnce(() => {
      throw new Error('Upload exceeds the 10 MB limit.')
    })
    const failSpy = vi.spyOn(store, 'failUpload')
    const wrapper = mountInput()

    const input = wrapper.find('[data-testid="composer-file-input"]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: makeFileList([new File(['hi'], 'a.txt', { type: 'text/plain' })]),
    })
    await input.trigger('change')
    await vi.waitFor(() => {
      expect(failSpy).toHaveBeenCalledTimes(1)
    })
    expect(failSpy.mock.calls[0]![1]).toContain('10 MB')
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(editorText(wrapper)).toBe('')
  })

  it('highlights the composer card while an external file drag is active', async () => {
    const wrapper = mountInput({ uploadDrag: { active: true, uploading: false } })
    const card = wrapper.find('[data-testid="composer-card"]')
    expect(card.classes()).toContain('border-primary')
    expect(card.classes()).toContain('ring-2')
    await wrapper.setProps({ uploadDrag: { active: false, uploading: false } })
    expect(card.classes()).toContain('border-border')
  })
})
