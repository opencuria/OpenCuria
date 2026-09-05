import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

import HarnessMarkdown from './HarnessMarkdown.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesRead: vi.fn(),
}))

function mountMarkdown(text: string, workspaceId = 'ws-1') {
  return mount(HarnessMarkdown, {
    props: { text },
    global: {
      provide: {
        [harnessWorkspaceIdKey as symbol]: ref(workspaceId),
      },
    },
  })
}

describe('HarnessMarkdown', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useWorkspaceImageStore()
    store.reset()
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

  it('still renders non-media markdown', () => {
    const wrapper = mountMarkdown('## Hello\n\nVisit [docs](https://example.com).')

    expect(wrapper.find('h2').text()).toBe('Hello')
    expect(wrapper.find('a').attributes('href')).toBe('https://example.com')
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
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
})
