import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import WorkspaceMedia from './WorkspaceMedia.vue'
import ImageLightbox from './ImageLightbox.vue'
import {
  makeFileLink,
  resetWorkspaceFiles,
  workspaceFiles,
} from '@/services/workspaceFiles.fixtures'
import { FileTransferError } from '@opencuria/client/files'
import { MEBIBYTE } from '@opencuria/chat-core/workspaceFiles'

vi.mock('@/services/workspaceFiles', () => import('@/services/workspaceFiles.fixtures'))

const files = workspaceFiles()
const IMAGE = '/workspace/shots/cat.png'
const VIDEO = '/workspace/clips/demo.mp4'

function withSize(size: number) {
  files.link.mockImplementation(async (_workspaceId, path) => makeFileLink(path, { size }))
}

function mountMedia(props: Record<string, unknown> = {}) {
  return mount(WorkspaceMedia, {
    props: { workspaceId: 'ws-1', path: IMAGE, kind: 'image', alt: 'Cat', ...props },
    slots: { fallback: '<span data-testid="fallback">missing</span>' },
    global: { stubs: { ImageLightbox: true } },
  })
}

describe('WorkspaceMedia', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetWorkspaceFiles()
  })

  it('streams small images from their link and opens the lightbox with the download URL', async () => {
    withSize(MEBIBYTE)
    const wrapper = mountMedia()
    expect(wrapper.find('[data-testid="workspace-media-loading"]').exists()).toBe(true)
    await flushPromises()

    expect(files.link).toHaveBeenCalledWith('ws-1', IMAGE, expect.any(AbortSignal))
    const image = wrapper.get('[data-testid="workspace-media-image"]')
    expect(image.attributes('src')).toBe(`https://files.test/content${IMAGE}`)
    expect(image.attributes('data-voice-image-path')).toBe(IMAGE)
    await wrapper.get('[data-testid="workspace-media-button"]').trigger('click')
    expect(wrapper.getComponent(ImageLightbox).props()).toMatchObject({
      src: `https://files.test/content${IMAGE}`,
      downloadUrl: `https://files.test/download${IMAGE}`,
      path: IMAGE,
      workspaceId: 'ws-1',
    })
  })

  it('renders a cached link synchronously without asking the server', () => {
    files.peekLink.mockReturnValue(makeFileLink(IMAGE))
    const wrapper = mountMedia()
    expect(wrapper.find('[data-testid="workspace-media-image"]').exists()).toBe(true)
    expect(files.link).not.toHaveBeenCalled()
  })

  it('shows the blurred preview and size of large images and loads them on tap', async () => {
    withSize(6 * MEBIBYTE)
    const wrapper = mountMedia()
    await flushPromises()

    const placeholder = wrapper.get('[data-testid="workspace-media-placeholder"]')
    expect(placeholder.attributes('data-mode')).toBe('tap')
    expect(placeholder.text()).toContain('6.0 MB')
    expect(placeholder.get('img').attributes('src')).toBe(`https://files.test/thumb${IMAGE}`)
    expect(wrapper.find('[data-testid="workspace-media-image"]').exists()).toBe(false)

    await wrapper.get('[data-testid="workspace-media-load"]').trigger('click')
    expect(wrapper.get('[data-testid="workspace-media-image"]').attributes('src')).toBe(
      `https://files.test/content${IMAGE}`,
    )

    // The choice survives re-renders for the session.
    const again = mountMedia()
    await flushPromises()
    expect(again.find('[data-testid="workspace-media-image"]').exists()).toBe(true)
    const otherWorkspace = mountMedia({ workspaceId: 'ws-2' })
    await flushPromises()
    expect(otherWorkspace.find('[data-testid="workspace-media-load"]').exists()).toBe(true)
  })

  it('keeps the thumbnail aspect ratio and falls back to a muted box without one', async () => {
    withSize(6 * MEBIBYTE)
    const wrapper = mountMedia()
    await flushPromises()
    const thumbnail = wrapper.get('[data-testid="workspace-media-placeholder"] img')
    Object.defineProperties(thumbnail.element, {
      naturalWidth: { value: 48 },
      naturalHeight: { value: 24 },
    })
    await thumbnail.trigger('load')
    const box = wrapper.get('[data-testid="workspace-media-placeholder"]').element as HTMLElement
    expect(box.style.aspectRatio).toBe('48 / 24')

    await thumbnail.trigger('error')
    expect(wrapper.find('[data-testid="workspace-media-placeholder"] img').exists()).toBe(false)
    expect(wrapper.find('[data-testid="workspace-media-load"]').exists()).toBe(true)
  })

  it('plays large videos after a tap without the blurred poster', async () => {
    withSize(50 * MEBIBYTE)
    const wrapper = mountMedia({ path: VIDEO, kind: 'video', alt: 'Demo' })
    await flushPromises()
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.get('[data-testid="workspace-media-load"]').attributes('aria-label')).toBe(
      'Play Demo (50.0 MB)',
    )

    await wrapper.get('[data-testid="workspace-media-load"]').trigger('click')
    const video = wrapper.get('[data-testid="workspace-media-video"]')
    expect(video.attributes('src')).toBe(`https://files.test/content${VIDEO}`)
    expect(video.attributes('poster')).toBeUndefined()
    expect(video.attributes('preload')).toBe('metadata')
    expect((video.element as HTMLVideoElement).autoplay).toBe(true)
  })

  it('streams small videos without autoplay', async () => {
    withSize(MEBIBYTE)
    const wrapper = mountMedia({ path: VIDEO, kind: 'video' })
    await flushPromises()
    const video = wrapper.get('[data-testid="workspace-media-video"]')
    expect(video.attributes('controls')).toBeDefined()
    expect((video.element as HTMLVideoElement).autoplay).toBe(false)
  })

  it('only previews files above the transfer limit', async () => {
    withSize(501 * MEBIBYTE)
    const wrapper = mountMedia()
    await flushPromises()
    const placeholder = wrapper.get('[data-testid="workspace-media-placeholder"]')
    expect(placeholder.attributes('data-mode')).toBe('too_large')
    expect(placeholder.text()).toContain('Too large to show (max 500 MB)')
    expect(wrapper.find('[data-testid="workspace-media-load"]').exists()).toBe(false)
  })

  it('renders the fallback for missing files and without a workspace', async () => {
    files.link.mockRejectedValue(
      new FileTransferError('The file does not exist.', 'file_not_found'),
    )
    const missing = mountMedia()
    await flushPromises()
    expect(missing.find('[data-testid="fallback"]').exists()).toBe(true)

    const detached = mountMedia({ workspaceId: '' })
    expect(detached.find('[data-testid="fallback"]').exists()).toBe(true)
    expect(files.link).toHaveBeenCalledTimes(1)
  })

  it('renews an expired link once before giving up', async () => {
    const wrapper = mountMedia()
    await flushPromises()
    await wrapper.get('[data-testid="workspace-media-image"]').trigger('error')
    await flushPromises()
    expect(files.forget).toHaveBeenCalledWith('ws-1', IMAGE)
    expect(files.link).toHaveBeenCalledTimes(2)

    await wrapper.get('[data-testid="workspace-media-image"]').trigger('error')
    expect(wrapper.find('[data-testid="fallback"]').exists()).toBe(true)
  })
})
