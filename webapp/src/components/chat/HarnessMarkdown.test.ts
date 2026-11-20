import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'
import { marked } from 'marked'

import HarnessMarkdown from './HarnessMarkdown.vue'
import ImageLightbox from './ImageLightbox.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useEditorStore } from '@/stores/editor'
import { useSidePanelStore } from '@/stores/sidePanel'
import WorkspaceEditor from '@/components/editor/WorkspaceEditor.vue'
import { readEditorFile } from '@/services/editor.api'
import {
  makeFileLink,
  resetWorkspaceFiles,
  workspaceFiles,
} from '@/services/workspaceFiles.fixtures'
import type { WorkspaceFileLink } from '@opencuria/chat-core/workspaceFiles'

vi.mock('@/services/socket', () => ({
  listWorkspaceFiles: vi.fn(async () => ({ entries: [] })),
  findWorkspaceFiles: vi.fn(),
}))
vi.mock('@/services/workspaceFiles', () => import('@/services/workspaceFiles.fixtures'))

const files = workspaceFiles()
const content = (path: string) => `https://files.test/content${path}`

vi.mock('@/services/editor.api', () => ({
  readEditorFile: vi.fn(() => new Promise(() => {})),
  saveEditorFile: vi.fn(),
}))

vi.mock('@/components/editor/MonacoEditor.vue', () => ({
  default: { name: 'MonacoEditor', template: '<div data-testid="monaco-editor" />' },
}))

function mountMarkdown(
  text: string,
  workspaceId = 'ws-1',
  extra: { compact?: boolean; onPrimary?: boolean; streaming?: boolean; baseDirectory?: string } = {},
) {
  return mount(HarnessMarkdown, {
    props: { text, ...extra },
    global: {
      stubs: { ImageLightbox: true },
      provide: {
        [harnessWorkspaceIdKey as symbol]: ref(workspaceId),
      },
    },
  })
}

function click(element: Element, options: MouseEventInit = {}): boolean {
  return element.dispatchEvent(
    new MouseEvent('click', { bubbles: true, cancelable: true, ...options }),
  )
}

describe('HarnessMarkdown', () => {
  beforeEach(() => {
    vi.useRealTimers()
    setActivePinia(createPinia())
    vi.clearAllMocks()
    resetWorkspaceFiles()
  })

  afterEach(() => {
    useFileExplorerStore().reset()
    useEditorStore().reset()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('removes the last wrapped block margin without changing paragraph spacing inside the message', () => {
    const wrapper = mountMarkdown('First paragraph\n\nFinal paragraph')
    expect(wrapper.get('.prose-output').classes()).toContain('[&>:last-child>:last-child]:mb-0')
    expect(wrapper.findAll('[data-md-html] > p').map((paragraph) => paragraph.text())).toEqual([
      'First paragraph',
      'Final paragraph',
    ])
  })

  it('coalesces markdown parsing during a stream and flushes the latest text on completion', async () => {
    vi.useFakeTimers()
    const parseSpy = vi.spyOn(marked, 'parse')
    const wrapper = mountMarkdown('First', 'ws-1', { streaming: true })
    const initialParseCount = parseSpy.mock.calls.length
    expect(wrapper.find('p').text()).toBe('First')

    await wrapper.setProps({ text: 'First update' })
    await wrapper.setProps({ text: 'First update final' })
    await nextTick()
    expect(parseSpy).toHaveBeenCalledTimes(initialParseCount)
    expect(wrapper.find('p').text()).toBe('First')

    await vi.advanceTimersByTimeAsync(50)
    await nextTick()
    expect(wrapper.find('p').text()).toBe('First update final')
    expect(parseSpy).toHaveBeenCalledTimes(initialParseCount + 1)

    await wrapper.setProps({ text: 'Final complete', streaming: false })
    expect(wrapper.find('p').text()).toBe('Final complete')
    expect(parseSpy).toHaveBeenCalledTimes(initialParseCount + 2)
    wrapper.unmount()
    parseSpy.mockRestore()
  })

  it('cancels a pending streaming render timer when unmounted', async () => {
    vi.useFakeTimers()
    const parseSpy = vi.spyOn(marked, 'parse')
    const wrapper = mountMarkdown('Before', 'ws-1', { streaming: true })
    const initialParseCount = parseSpy.mock.calls.length
    await wrapper.setProps({ text: 'After' })
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(50)
    expect(parseSpy).toHaveBeenCalledTimes(initialParseCount)
    parseSpy.mockRestore()
  })

  it('renders a video element streaming from the workspace link', async () => {
    const wrapper = mountMarkdown('![clip](/workspace/a.mp4)')
    await flushPromises()

    const video = wrapper.get('video')
    expect(video.attributes('src')).toBe(content('/workspace/a.mp4'))
    expect(video.attributes('controls')).toBeDefined()
    expect(video.attributes('preload')).toBe('metadata')
  })

  it('renders an image element streaming from the workspace link', async () => {
    const wrapper = mountMarkdown('![pic](/workspace/a.png)')
    await flushPromises()

    const image = wrapper.get('img')
    expect(image.attributes('src')).toBe(content('/workspace/a.png'))
    expect(image.attributes('alt')).toBe('pic')
  })

  it('resolves relative image paths to the same workspace path as absolute', async () => {
    const relative = mountMarkdown('![pic](a.png)')
    const dotted = mountMarkdown('![pic](./a.png)')
    await flushPromises()

    expect(files.link.mock.calls.map((call) => call[1])).toEqual([
      '/workspace/a.png',
      '/workspace/a.png',
    ])
    expect(relative.get('img').attributes('src')).toBe(content('/workspace/a.png'))
    expect(dotted.get('img').attributes('src')).toBe(content('/workspace/a.png'))
    expect(relative.get('img').attributes('alt')).toBe('pic')
  })

  it('requests images with spaces in their name by their decoded file path', async () => {
    mountMarkdown('![a](/workspace/shots/my%20shot.png)')
    mountMarkdown('![b](<shots/my shot.png>)')
    mountMarkdown('<img src="/workspace/shots/my shot.png" alt="c">')
    mountMarkdown('![d](/workspace/shots/100%25%20done%231.png)')
    await flushPromises()

    expect(files.link.mock.calls.map((call) => call[1])).toEqual([
      '/workspace/shots/my shot.png',
      '/workspace/shots/my shot.png',
      '/workspace/shots/my shot.png',
      '/workspace/shots/100% done#1.png',
    ])
  })

  it('shows a tap-to-load preview for large media', async () => {
    files.link.mockImplementation(async (_workspaceId: string, path: string) =>
      makeFileLink(path, { size: 20 * 1024 * 1024 }),
    )
    const wrapper = mountMarkdown('![Big](/workspace/big.png)')
    await flushPromises()

    expect(wrapper.find('[data-testid="workspace-media-image"]').exists()).toBe(false)
    await wrapper.get('[data-testid="workspace-media-load"]').trigger('click')
    expect(wrapper.get('[data-testid="workspace-media-image"]').attributes('src')).toBe(
      content('/workspace/big.png'),
    )
  })

  it('leaves remote markdown images to regular HTML rendering', () => {
    const wrapper = mountMarkdown('![pic](https://example.com/a.png)')

    const image = wrapper.find('img')
    expect(image.exists()).toBe(true)
    expect(image.attributes('src')).toBe('https://example.com/a.png')
    expect(files.link).not.toHaveBeenCalled()
  })

  it('resolves document images, HTML images and reference images from their directory', async () => {
    const wrapper = mountMarkdown(
      '![Relative](./images/photo.png)\n\n![Reference][photo]\n\n' +
      '[photo]: ../assets/Photo%20%C3%A4.png?v=1#preview\n\n' +
      '<img src="/workspace/shared.svg" alt="Absolute">',
      'ws-1',
      { baseDirectory: '/workspace/repo/docs' },
    )
    await flushPromises()
    expect(wrapper.findAll('img').map((img) => img.attributes('src'))).toEqual([
      content('/workspace/repo/docs/images/photo.png'),
      content('/workspace/repo/assets/Photo ä.png'),
      content('/workspace/shared.svg'),
    ])
    wrapper.unmount()
  })

  it('opens document links relative to their directory and updates media when the directory changes', async () => {
    const explorer = useFileExplorerStore()
    const openPath = vi.spyOn(explorer, 'openPath').mockResolvedValue(undefined)
    const wrapper = mountMarkdown('![Local](photo.png)\n\n[Guide](../guide.md)', 'ws-1', {
      baseDirectory: '/workspace/repo/docs',
    })
    expect(wrapper.get('a').attributes('target')).toBeUndefined()
    click(wrapper.get('a').element)
    expect(openPath).toHaveBeenCalledWith('ws-1', '/workspace/repo/guide.md', { revealInExplorer: false })
    expect(files.link).toHaveBeenCalledWith(
      'ws-1',
      '/workspace/repo/docs/photo.png',
      expect.any(AbortSignal),
    )

    await wrapper.setProps({ baseDirectory: '/workspace/other' })
    expect(files.link).toHaveBeenCalledWith(
      'ws-1',
      '/workspace/other/photo.png',
      expect.any(AbortSignal),
    )
    await flushPromises()
    expect(wrapper.get('img').attributes('src')).toBe(content('/workspace/other/photo.png'))
    wrapper.unmount()
  })

  it('keeps document media sanitized and never fetches paths outside the workspace', async () => {
    const wrapper = mountMarkdown(
      '![Escape](../../../private.png)\n\n![Remote](https://example.com/photo.png)\n\n' +
      '<img src="javascript:alert(1)" onerror="alert(1)" alt="Unsafe">',
      'ws-1',
      { baseDirectory: '/workspace/repo/docs' },
    )
    await flushPromises()
    expect(files.link).not.toHaveBeenCalled()
    expect(wrapper.html()).not.toContain('javascript:')
    expect(wrapper.html()).not.toContain('onerror')
    expect(wrapper.get('img[alt="Remote"]').attributes('src')).toBe('https://example.com/photo.png')
    wrapper.unmount()
  })

  it('still renders non-media markdown', () => {
    const wrapper = mountMarkdown('## Hello\n\nVisit [docs](https://example.com).')

    expect(wrapper.find('h2').text()).toBe('Hello')
    expect(wrapper.find('a').attributes('href')).toBe('https://example.com')
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.get('.prose-output > div').find('h2').exists()).toBe(true)
  })

  it.each([
    'sb-laden-konzept/sb-laden-konzept.pdf',
    '/workspace/sb-laden-konzept/sb-laden-konzept.pdf',
    './sb-laden-konzept/../sb-laden-konzept/sb-laden-konzept.pdf?download=1#page=2',
  ])('opens a PDF editor tab from a formatted file link: %s', async (href) => {
    const path = '/workspace/sb-laden-konzept/sb-laden-konzept.pdf'
    const store = useFileExplorerStore()
    useSidePanelStore().close()
    let resolveLink!: (value: WorkspaceFileLink) => void
    files.link.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveLink = resolve
      }),
    )
    const wrapper = mountMarkdown(`**[Aktualisierte PDF öffnen](${href})**`)
    const viewer = mount(WorkspaceEditor)

    expect(wrapper.get('strong a').text()).toBe('Aktualisierte PDF öffnen')
    expect(wrapper.get('a').attributes('target')).toBeUndefined()
    expect(files.link).not.toHaveBeenCalled()
    expect(click(wrapper.get('strong a').element)).toBe(false)
    await flushPromises()
    expect(useSidePanelStore().isOpen).toBe(false)
    expect(store.selectedPath).toBe(path)
    expect(files.link).toHaveBeenCalledWith('ws-1', path, expect.any(AbortSignal))
    expect(viewer.find('[data-testid="editor-loading"]').exists()).toBe(true)

    resolveLink(makeFileLink(path))
    await flushPromises()
    expect(useEditorStore().activeTab!.name).toBe('sb-laden-konzept.pdf')
    expect(viewer.get('iframe[title="PDF preview"]').attributes('src')).toBe(content(path))
    expect(useEditorStore().closeTab(useEditorStore().activeTab!.id)).toBe(true)
    await nextTick()
    expect(viewer.find('[data-testid="workspace-editor"]').exists()).toBe(false)
    wrapper.unmount()
    viewer.unmount()
  })

  it.each([
    ['[File](docs/Updated%20plan%20%C3%A4.txt)', '/workspace/docs/Updated plan ä.txt'],
    ['[File](<docs/Updated plan.txt>)', '/workspace/docs/Updated plan.txt'],
    ['[File](docs/100%25%20done%23final%3F.txt)', '/workspace/docs/100% done#final?.txt'],
    ['[File](docs/%252e%252e.txt)', '/workspace/docs/%2e%2e.txt'],
    ['[File][doc]\n\n[doc]: /workspace/README.md "Read me"', '/workspace/README.md'],
  ])('reads the exact decoded filename for %s', async (text, path) => {
    const wrapper = mountMarkdown(text)
    await wrapper.get('a').trigger('click')
    await flushPromises()
    expect(readEditorFile).toHaveBeenCalledWith('ws-1', path, expect.any(AbortSignal))
    wrapper.unmount()
  })

  it('shows editor errors when a linked file cannot be read', async () => {
    files.link.mockRejectedValueOnce(new Error('Permission denied'))
    const wrapper = mountMarkdown('[Missing](missing.pdf)')
    const viewer = mount(WorkspaceEditor)
    await wrapper.get('a').trigger('click')
    await flushPromises()
    expect(viewer.get('[data-testid="editor-error"]').text()).toContain('Permission denied')
    expect(viewer.find('iframe').exists()).toBe(false)
    wrapper.unmount()
    viewer.unmount()
  })

  it.each([
    'https://example.com/plan.pdf',
    '//example.com/plan.pdf',
    'mailto:a@example.com',
    '#section',
    '?query=only',
    '../etc/passwd',
    '/tmp/plan.pdf',
    '/workspace/%2e%2e/etc/passwd',
    'bad%ZZ.pdf',
    'javascript:alert(1)',
  ])('does not intercept non-workspace or invalid links: %s', async (href) => {
    const openPath = vi.spyOn(useFileExplorerStore(), 'openPath')
    const wrapper = mountMarkdown(`[Link](${href})`)
    expect(click(wrapper.get('a').element)).toBe(true)
    expect(openPath).not.toHaveBeenCalled()
    expect(files.link).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('leaves links alone without workspace context', async () => {
    const openPath = vi.spyOn(useFileExplorerStore(), 'openPath')
    const wrapper = mountMarkdown('[File](plan.pdf)', '')
    expect(wrapper.get('a').attributes('target')).toBe('_blank')
    expect(click(wrapper.get('a').element)).toBe(true)
    expect(openPath).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { altKey: true },
    { button: 1 },
  ])('preserves modified link navigation: %j', async (modifiers) => {
    const openPath = vi.spyOn(useFileExplorerStore(), 'openPath')
    const wrapper = mountMarkdown('[File](plan.pdf)')
    expect(click(wrapper.get('a').element, modifiers)).toBe(true)
    expect(openPath).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('opens links when their formatted child is activated and uses the current workspace', async () => {
    const openPath = vi.spyOn(useFileExplorerStore(), 'openPath').mockResolvedValue()
    const workspaceId = ref('ws-1')
    const wrapper = mount(HarnessMarkdown, {
      props: { text: '[**File**](plan.pdf)' },
      global: { provide: { [harnessWorkspaceIdKey as symbol]: workspaceId } },
    })
    workspaceId.value = 'ws-2'
    click(wrapper.get('a strong').element, { detail: 0 })
    expect(openPath).toHaveBeenCalledWith('ws-2', '/workspace/plan.pdf', {
      revealInExplorer: false,
    })
    wrapper.unmount()
  })

  it('shows a loading placeholder while the media link resolves', () => {
    files.link.mockReturnValue(new Promise(() => {}))
    const wrapper = mountMarkdown('![clip](/workspace/a.mp4)')

    expect(wrapper.find('[data-testid="workspace-media-loading"]').exists()).toBe(true)
    expect(wrapper.find('video').exists()).toBe(false)
  })

  it('falls back to alt text when the media file is missing', async () => {
    files.link.mockRejectedValue(new Error('The file does not exist.'))
    const wrapper = mountMarkdown('![clip](/workspace/a.mp4)')
    await flushPromises()

    expect(wrapper.get('[data-testid="harness-markdown-media-fallback"]').text()).toBe('clip')
  })

  it('falls back to alt text when workspace id is missing', () => {
    const wrapper = mountMarkdown('![clip](/workspace/a.mp4)', '')

    expect(wrapper.find('[data-testid="harness-markdown-media-fallback"]').text()).toBe('clip')
    expect(wrapper.find('video').exists()).toBe(false)
  })

  it('applies on-primary prose classes instead of foreground modifiers', () => {
    const wrapper = mountMarkdown('Hello **world**', 'ws-1', { onPrimary: true })
    const classes = wrapper.get('.prose-output').classes()

    expect(classes).toContain('prose-on-primary')
    expect(classes).toContain('prose-p:text-primary-foreground')
    expect(classes).not.toContain('prose-p:text-foreground')
    expect(classes).not.toContain('dark:prose-invert')
  })

  it('opens the shared lightbox for workspace images and closes it again', async () => {
    const wrapper = mountMarkdown('![Photo](photo.png)')
    await flushPromises()
    const trigger = wrapper.get('button[aria-haspopup="dialog"]')

    expect(trigger.attributes('aria-label')).toBe('Open image preview Photo')
    await trigger.trigger('click')
    expect(wrapper.getComponent(ImageLightbox).props()).toEqual({
      src: content('/workspace/photo.png'),
      alt: 'Photo',
      path: '/workspace/photo.png',
      workspaceId: 'ws-1',
      downloadUrl: 'https://files.test/download/workspace/photo.png',
    })
    wrapper.getComponent(ImageLightbox).vm.$emit('close')
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(false)
    expect(wrapper.get('img').attributes('alt')).toBe('Photo')
    wrapper.unmount()
  })

  it.each(['click', 'Enter', ' '])('opens sanitized remote images via %s', async (activation) => {
    const wrapper = mountMarkdown('![Remote photo](https://example.com/photo.png)')
    const image = wrapper.get('img')

    expect(image.attributes('role')).toBe('button')
    expect(image.attributes('tabindex')).toBe('0')
    expect(image.attributes('aria-label')).toBe('Open image preview Remote photo')
    await image.trigger(activation === 'click' ? 'click' : 'keydown', { key: activation })
    expect(wrapper.getComponent(ImageLightbox).props('src')).toBe('https://example.com/photo.png')
    expect(files.link).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps linked images as links rather than overriding their navigation', async () => {
    const wrapper = mountMarkdown(
      '[![Photo](https://example.com/photo.png)](https://example.com/full)',
    )

    expect(wrapper.get('a').attributes('target')).toBe('_blank')
    expect(wrapper.get('a').attributes('rel')).toBe('noopener noreferrer')
    expect(wrapper.get('img').attributes('role')).toBeUndefined()
    await wrapper.get('img').trigger('click')
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(false)
    wrapper.unmount()
  })

  it('loads linked workspace media while preserving the original link and avoiding lightbox controls', async () => {
    const wrapper = mountMarkdown('[![Photo](/workspace/photo.png)](https://example.com/full)')

    expect(files.link).toHaveBeenCalledWith('ws-1', '/workspace/photo.png', expect.any(AbortSignal))
    await flushPromises()

    const link = wrapper.get('a')
    const image = wrapper.get('img')
    expect(link.attributes('href')).toBe('https://example.com/full')
    expect(image.attributes('src')).toBe(content('/workspace/photo.png'))
    expect(image.attributes('role')).toBeUndefined()
    expect(wrapper.find('button[aria-haspopup="dialog"]').exists()).toBe(false)

    await image.trigger('click')
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(false)
    expect(files.link).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('does not turn decorative mention icons into preview triggers', async () => {
    const wrapper = mountMarkdown('@file:/workspace/photo.png')
    await wrapper.setProps({ mentions: true })
    await wrapper.vm.$nextTick()
    const icon = wrapper.get('[data-testid="mention-badge-icon"]')

    await icon.trigger('click')
    await icon.trigger('keydown', { key: 'Enter' })
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(false)
    expect(icon.attributes('role')).toBeUndefined()
    expect(files.link).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not offer a lightbox for unavailable or loading workspace images', async () => {
    const unavailable = mountMarkdown('![Missing](missing.png)', '')
    expect(unavailable.get('[data-testid="harness-markdown-media-fallback"]').text()).toBe(
      'Missing',
    )
    expect(unavailable.find('button').exists()).toBe(false)
    expect(unavailable.findComponent(ImageLightbox).exists()).toBe(false)

    files.link.mockReturnValue(new Promise(() => {}))
    const loading = mountMarkdown('![Loading](loading.png)')
    expect(loading.find('[data-testid="workspace-media-loading"]').exists()).toBe(true)
    expect(loading.find('button').exists()).toBe(false)
    unavailable.unmount()
    loading.unmount()
  })

  it('does not bypass markdown sanitization to make unsafe sources previewable', async () => {
    const wrapper = mountMarkdown('<img src="javascript:alert(1)" onerror="alert(1)" alt="Unsafe">')
    const image = wrapper.get('img')

    expect(image.attributes('src')).toBeUndefined()
    expect(image.attributes('onerror')).toBeUndefined()
    expect(image.attributes('role')).toBeUndefined()
    await image.trigger('click')
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(false)
    wrapper.unmount()
  })
})
