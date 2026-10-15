import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'
import { marked } from 'marked'

import HarnessMarkdown from './HarnessMarkdown.vue'
import ImageLightbox from './ImageLightbox.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesRead: vi.fn(),
}))

function mountMarkdown(
  text: string,
  workspaceId = 'ws-1',
  extra: { compact?: boolean; onPrimary?: boolean; streaming?: boolean } = {},
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

describe('HarnessMarkdown', () => {
  beforeEach(() => {
    vi.useRealTimers()
    setActivePinia(createPinia())
    const store = useWorkspaceImageStore()
    store.reset()
    vi.mocked(sendFilesRead).mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
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

  it('releases visible media pins when a markdown segment is removed', async () => {
    const store = useWorkspaceImageStore()
    const payload = Buffer.alloc(20 * 1024 * 1024, 1).toString('base64')
    store.imageCache['/workspace/a.png'] = `data:image/png;base64,${payload}`
    const wrapper = mountMarkdown('![a](/workspace/a.png)')
    await nextTick()
    store.storeLocalImage('/workspace/b.png', `data:image/png;base64,${payload}`)

    await wrapper.setProps({ text: 'No more media' })
    await nextTick()

    expect(store.imageCache['/workspace/a.png']).toBeUndefined()
    wrapper.unmount()
  })

  it('reacquires markdown media pins after store reset without stale unpinning', async () => {
    const store = useWorkspaceImageStore()
    const payload = Buffer.alloc(20 * 1024 * 1024, 1).toString('base64')
    store.pinMedia('/workspace/a.png', true)
    store.storeLocalImage('/workspace/a.png', `data:image/png;base64,${payload}`)
    const wrapper = mountMarkdown('![a](/workspace/a.png)')
    store.pinMedia('/workspace/a.png', false)
    await nextTick()
    expect(store.getImageUrl('/workspace/a.png')).not.toBeNull()

    store.reset()
    await nextTick()
    expect(store.getImageUrl('/workspace/a.png')).toBeNull()
    const calls = vi.mocked(sendFilesRead).mock.calls
    const requestId = calls[calls.length - 1]![1]
    store.handleContentResult(requestId, '/workspace/a.png', payload, undefined, 'image/png')
    expect(store.imageCache['/workspace/a.png']).toBeDefined()

    store.storeLocalImage('/workspace/b.png', `data:image/png;base64,${payload}`)
    expect(store.imageCache['/workspace/a.png']).toBeDefined()
    wrapper.unmount()
    expect(store.getImageUrl('/workspace/a.png')).toBeNull()
  })

  it('renders a video element when the store has a cached URL', () => {
    const store = useWorkspaceImageStore()
    store.videoCache['/workspace/a.mp4'] = 'blob:video-test'
    store.videoMimeTypes['/workspace/a.mp4'] = 'video/mp4'

    const wrapper = mountMarkdown('![clip](/workspace/a.mp4)')

    const video = wrapper.find('video')
    expect(video.exists()).toBe(true)
    expect(video.attributes('src')).toBe('blob:video-test')
    expect(video.attributes('controls')).toBeDefined()
  })

  it('renders an image element when the store has a cached URL', () => {
    const store = useWorkspaceImageStore()
    store.imageCache['/workspace/a.png'] = 'data:image/png;base64,abc'

    const wrapper = mountMarkdown('![pic](/workspace/a.png)')

    const image = wrapper.find('img')
    expect(image.exists()).toBe(true)
    expect(image.attributes('src')).toBe('data:image/png;base64,abc')
    expect(image.attributes('alt')).toBe('pic')
  })

  it('resolves relative image paths to the same cache key as absolute', () => {
    const store = useWorkspaceImageStore()
    store.imageCache['/workspace/a.png'] = 'data:image/png;base64,abc'

    const relative = mountMarkdown('![pic](a.png)')
    const dotted = mountMarkdown('![pic](./a.png)')

    expect(relative.find('img').attributes('src')).toBe('data:image/png;base64,abc')
    expect(dotted.find('img').attributes('src')).toBe('data:image/png;base64,abc')
    expect(relative.find('img').attributes('alt')).toBe('pic')
  })

  it('leaves remote markdown images to regular HTML rendering', () => {
    const wrapper = mountMarkdown('![pic](https://example.com/a.png)')

    const image = wrapper.find('img')
    expect(image.exists()).toBe(true)
    expect(image.attributes('src')).toBe('https://example.com/a.png')
    expect(sendFilesRead).not.toHaveBeenCalled()
  })

  it('still renders non-media markdown', () => {
    const wrapper = mountMarkdown('## Hello\n\nVisit [docs](https://example.com).')

    expect(wrapper.find('h2').text()).toBe('Hello')
    expect(wrapper.find('a').attributes('href')).toBe('https://example.com')
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.get('.prose-output > div').find('h2').exists()).toBe(true)
  })

  it('shows a loading placeholder while media is fetching', () => {
    const store = useWorkspaceImageStore()
    store.fetchingVideos['/workspace/a.mp4'] = true

    const wrapper = mountMarkdown('![clip](/workspace/a.mp4)')

    expect(wrapper.find('[data-testid="harness-markdown-media-loading"]').exists()).toBe(true)
    expect(wrapper.find('video').exists()).toBe(false)
    expect(sendFilesRead).not.toHaveBeenCalled()
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
    useWorkspaceImageStore().imageCache['/workspace/photo.png'] = 'data:image/png;base64,abc'
    const wrapper = mountMarkdown('![Photo](photo.png)')
    const trigger = wrapper.get('button[aria-haspopup="dialog"]')

    expect(trigger.attributes('aria-label')).toBe('Open image preview Photo')
    await trigger.trigger('click')
    expect(wrapper.getComponent(ImageLightbox).props()).toEqual({
      src: 'data:image/png;base64,abc',
      alt: 'Photo',
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
    expect(sendFilesRead).not.toHaveBeenCalled()
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
    const store = useWorkspaceImageStore()
    const wrapper = mountMarkdown('[![Photo](/workspace/photo.png)](https://example.com/full)')

    expect(sendFilesRead).toHaveBeenCalledWith('ws-1', expect.any(String), '/workspace/photo.png')
    store.imageCache['/workspace/photo.png'] = 'data:image/png;base64,abc'
    await wrapper.vm.$nextTick()

    const link = wrapper.get('a')
    const image = wrapper.get('img')
    expect(link.attributes('href')).toBe('https://example.com/full')
    expect(image.attributes('src')).toBe('data:image/png;base64,abc')
    expect(image.attributes('role')).toBeUndefined()
    expect(wrapper.find('button[aria-haspopup="dialog"]').exists()).toBe(false)

    vi.mocked(sendFilesRead).mockClear()
    await image.trigger('click')
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(false)
    expect(sendFilesRead).not.toHaveBeenCalled()
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
    expect(sendFilesRead).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not offer a lightbox for unavailable or loading workspace images', async () => {
    const unavailable = mountMarkdown('![Missing](missing.png)', '')
    expect(unavailable.get('[data-testid="harness-markdown-media-fallback"]').text()).toBe(
      'Missing',
    )
    expect(unavailable.find('button').exists()).toBe(false)
    expect(unavailable.findComponent(ImageLightbox).exists()).toBe(false)

    useWorkspaceImageStore().fetchingPaths['/workspace/loading.png'] = true
    const loading = mountMarkdown('![Loading](loading.png)')
    expect(loading.find('[data-testid="harness-markdown-media-loading"]').exists()).toBe(true)
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
