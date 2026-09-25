import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'

import HarnessMarkdown from './HarnessMarkdown.vue'
import HarnessMentionImages, {
  type MentionFileToken,
} from './HarnessMentionImages.vue'
import ImageLightbox from './ImageLightbox.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesRead: vi.fn(),
}))

function fileToken(path: string, overrides: Partial<MentionFileToken> = {}): MentionFileToken {
  const raw = `@file:${path}`
  return {
    kind: 'file',
    path,
    raw,
    name: path.split('/').pop() ?? path,
    start: 0,
    end: raw.length,
    ...overrides,
  }
}

function mountMarkdown(
  text: string,
  workspaceId = 'ws-mentions',
  extra: { compact?: boolean; onPrimary?: boolean; mentions?: boolean } = {},
) {
  return mount(HarnessMarkdown, {
    props: { text, ...extra },
    attachTo: document.body,
    global: {
      provide: {
        [harnessWorkspaceIdKey as symbol]: ref(workspaceId),
      },
      stubs: { teleport: true },
    },
  })
}

async function flushBadges(): Promise<void> {
  await nextTick()
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 60))
}

describe('HarnessMarkdown mentions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useWorkspaceImageStore().reset()
    vi.mocked(sendFilesRead).mockClear()
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders file and agent tokens as inline colored badges in user history', async () => {
    const wrapper = mountMarkdown(
      'Fix @file:/workspace/src/a.ts with @agent:plan now',
      'ws-mentions',
      { onPrimary: true, mentions: true },
    )
    await flushBadges()

    const files = wrapper.findAll('[data-testid="mention-badge-file"]')
    expect(files).toHaveLength(1)
    // Badge shows filename only; full path stays in the title tooltip.
    expect(files[0]!.text()).toBe('a.ts')
    expect(files[0]!.text()).not.toContain('src/a.ts')
    expect(files[0]!.attributes('data-path')).toBe('/workspace/src/a.ts')
    expect(files[0]!.attributes('title')).toBe('/workspace/src/a.ts')
    expect(files[0]!.classes().join(' ')).toContain('bg-primary-foreground/15')
    expect(files[0]!.find('[data-testid="mention-badge-icon"]').exists()).toBe(true)

    const agents = wrapper.findAll('[data-testid="mention-badge-agent"]')
    expect(agents).toHaveLength(1)
    expect(agents[0]!.text()).toBe('@agent:plan')
    expect(agents[0]!.attributes('data-agent')).toBe('plan')
    expect(agents[0]!.classes().join(' ')).toContain('text-primary-foreground')

    // The raw `@file:` text is replaced by the badge; the agent badge keeps
    // its `@agent:name` label (query by data-testid, not raw text).
    expect(wrapper.text()).not.toContain('@file:/workspace/src/a.ts')
    expect(wrapper.find('[data-testid="mention-badge-agent"]').text()).toBe('@agent:plan')
    expect(wrapper.text()).toContain('now')
  })

  it('badges non-image files inline without a thumbnail strip', async () => {
    const wrapper = mountMarkdown('Read @file:/workspace/src/a.ts please', 'ws-mentions', {
      mentions: true,
    })
    await flushBadges()

    expect(wrapper.findAll('[data-testid="mention-badge-file"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
    expect(vi.mocked(sendFilesRead)).not.toHaveBeenCalled()
  })

  it('shows an image thumbnail above the text and opens the lightbox', async () => {
    const store = useWorkspaceImageStore()
    store.imageCache['/workspace/cat.png'] = 'data:image/png;base64,abc'

    const wrapper = mountMarkdown('Look @file:/workspace/cat.png cute', 'ws-mentions', {
      mentions: true,
    })
    await flushBadges()

    const strip = wrapper.find('[data-testid="mention-images"]')
    expect(strip.exists()).toBe(true)
    const thumb = wrapper.find('[data-testid="mention-image"]')
    expect(thumb.exists()).toBe(true)
    expect(thumb.attributes('src')).toBe('data:image/png;base64,abc')
    // Thumbnail sits above the markdown text (first in DOM order).
    expect(strip.element.compareDocumentPosition(wrapper.get('[data-md-html]').element)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )

    await wrapper.get('[data-testid="mention-image-button"]').trigger('click')
    const lightbox = wrapper.findComponent(ImageLightbox)
    expect(lightbox.exists()).toBe(true)
    expect(lightbox.props('src')).toBe('data:image/png;base64,abc')
  })

  it('fetches the mention image when the cache is cold', async () => {
    mountMarkdown('Look @file:/workspace/cat.png', 'ws-mentions', {
      mentions: true,
    })
    await flushBadges()

    expect(vi.mocked(sendFilesRead)).toHaveBeenCalled()
    const [, , path] = vi.mocked(sendFilesRead).mock.calls[0]!
    expect(path).toBe('/workspace/cat.png')
  })

  it('shows a fallback tile when the image is absent and never fetches without a workspace', async () => {
    const wrapper = mountMarkdown('Look @file:/workspace/cat.png', '', { mentions: true })
    await flushBadges()

    expect(wrapper.find('[data-testid="mention-image-fallback"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mention-image-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="mention-image"]').exists()).toBe(false)
    expect(vi.mocked(sendFilesRead)).not.toHaveBeenCalled()
  })

  it('ignores sandbox escapes and malformed tokens', async () => {
    const wrapper = mountMarkdown(
      'Bad @file:/workspace/../etc/passwd and @file: plus @agent: plus @file:not-a-path stuck@email.com',
      'ws-mentions',
      { mentions: true },
    )
    await flushBadges()

    expect(wrapper.findAll('[data-testid="mention-badge-file"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="mention-badge-agent"]')).toHaveLength(0)
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('stuck@email.com')
    expect(vi.mocked(sendFilesRead)).not.toHaveBeenCalled()
  })

  it('leaves code blocks and assistant responses unchanged', async () => {
    const code = mountMarkdown('`@file:/workspace/a.ts` and `@agent:plan`', 'ws-mentions', {
      mentions: true,
    })
    await flushBadges()
    expect(code.findAll('[data-testid="mention-badge-file"]')).toHaveLength(0)
    expect(code.findAll('[data-testid="mention-badge-agent"]')).toHaveLength(0)
    expect(code.find('code').text()).toContain('@file:/workspace/a.ts')
    expect(code.find('[data-testid="mention-images"]').exists()).toBe(false)
    const imageCode = mountMarkdown('`@file:/workspace/secret.png`', 'ws-mentions', { mentions: true })
    await flushBadges()
    expect(imageCode.find('[data-testid="mention-images"]').exists()).toBe(false)
    expect(vi.mocked(sendFilesRead)).not.toHaveBeenCalled()

    const assistant = mountMarkdown('Fix @file:/workspace/a.ts with @agent:plan', 'ws-mentions')
    await flushBadges()
    expect(assistant.findAll('[data-testid="mention-badge-file"]')).toHaveLength(0)
    expect(assistant.findAll('[data-testid="mention-badge-agent"]')).toHaveLength(0)
    expect(assistant.text()).toContain('@file:/workspace/a.ts')
    expect(assistant.find('[data-testid="mention-images"]').exists()).toBe(false)
  })

  it('preserves surrounding markdown formatting', async () => {
    const wrapper = mountMarkdown('**Bold** @file:/workspace/a.ts with [docs](https://example.com)', 'ws-mentions', {
      mentions: true,
    })
    await flushBadges()

    expect(wrapper.find('strong').text()).toBe('Bold')
    expect(wrapper.find('a').attributes('href')).toBe('https://example.com')
    expect(wrapper.findAll('[data-testid="mention-badge-file"]')).toHaveLength(1)
  })
})

describe('HarnessMentionImages', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useWorkspaceImageStore().reset()
    vi.mocked(sendFilesRead).mockClear()
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function mountStrip(
    tokens: MentionFileToken[],
    workspaceId = 'ws-strip',
    extra: { removable?: boolean; onPrimary?: boolean } = {},
  ) {
    return mount(HarnessMentionImages, {
      props: { tokens, workspaceId, ...extra },
      attachTo: document.body,
      global: {
        provide: {
          [harnessWorkspaceIdKey as symbol]: ref(workspaceId),
        },
        stubs: { teleport: true },
      },
    })
  }

  it('renders a loading skeleton while the image fetches', () => {
    const store = useWorkspaceImageStore()
    store.fetchingPaths['/workspace/cat.png'] = true

    const wrapper = mountStrip([fileToken('/workspace/cat.png')])
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mention-image-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mention-image"]').exists()).toBe(false)
  })

  it('ignores non-image and escaping tokens without fetching', () => {
    const wrapper = mountStrip(
      [
        fileToken('/workspace/a.ts'),
        fileToken('/workspace/../etc/passwd'),
        { kind: 'file', path: '/etc/passwd', start: 0, end: 5 },
      ],
      'ws-strip-absent',
    )

    expect(wrapper.html()).not.toContain('mention-images')
    expect(vi.mocked(sendFilesRead)).not.toHaveBeenCalled()
  })

  it('has no remove affordance in history, but emits offsets when removable', async () => {
    const history = mountStrip([fileToken('/workspace/cat.png')], 'ws-history')
    expect(history.find('[data-testid="mention-images"]').exists()).toBe(true)
    expect(history.find('[data-testid="mention-image-remove"]').exists()).toBe(false)

    const composer = mountStrip([fileToken('/workspace/cat.png', { start: 5, end: 31 })], 'ws-strip', {
      removable: true,
    })
    await composer.find('[data-testid="mention-image-remove"]').trigger('click')
    expect(composer.emitted('remove')).toEqual([[5, 31]])
  })
})
