import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'

import HarnessMessageView from './HarnessMessageView.vue'
import type { HarnessMessage } from '@/types/harness'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'
import { sendFilesRead } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesRead: vi.fn(),
}))

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listProviderModels: vi.fn().mockResolvedValue([]),
  }
})

function makeUser(content: string): HarnessMessage {
  return {
    id: 'user-1',
    session_id: 'session-1',
    role: 'user',
    content,
    parts: [],
  }
}

function makeAssistant(content: string): HarnessMessage {
  return {
    id: 'assistant-1',
    session_id: 'session-1',
    role: 'assistant',
    content,
    parts: [
      {
        id: 'part-text-1',
        session_id: 'session-1',
        type: 'text',
        state: 'completed',
        title: '',
        output: content,
      },
    ],
  }
}

function mountView(message: HarnessMessage, workspaceId = 'ws-history') {
  return mount(HarnessMessageView, {
    props: { message },
    attachTo: document.body,
    global: {
      provide: {
        [harnessWorkspaceIdKey as symbol]: ref(workspaceId),
      },
      stubs: { teleport: true },
    },
  })
}

async function flush(): Promise<void> {
  await nextTick()
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 60))
}

describe('HarnessMessageView mentions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useWorkspaceImageStore().reset()
    resetProviderCatalogCache()
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

  it('renders mention badges and image thumbnails in user history without a remove affordance', async () => {
    useWorkspaceImageStore().imageCache['/workspace/cat.png'] =
      'data:image/png;base64,abc'
    const wrapper = mountView(
      makeUser('Fix @file:/workspace/src/a.ts with @agent:build, see @file:/workspace/cat.png'),
    )
    await flush()

    expect(wrapper.findAll('[data-testid="mention-badge-file"]').length).toBeGreaterThanOrEqual(
      1,
    )
    expect(wrapper.find('[data-testid="mention-badge-agent"]').text()).toBe('@agent:build')
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mention-image"]').attributes('src')).toBe(
      'data:image/png;base64,abc',
    )
    expect(wrapper.find('[data-testid="mention-image-remove"]').exists()).toBe(false)
    const bubble = wrapper.get('.bg-primary')
    const strip = wrapper.get('[data-testid="mention-images"]')
    expect(bubble.element.compareDocumentPosition(strip.element)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(bubble.element.contains(strip.element)).toBe(false)
    expect(strip.classes()).toContain('justify-end')
    expect(wrapper.get('[data-testid="mention-badge-file"]').classes()).toContain('h-5')
  })

  it('updates thumbnails when a user message changes and hides them during editing', async () => {
    const wrapper = mountView(makeUser('See @file:/workspace/old.png'))
    await flush()
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(true)
    await wrapper.setProps({ message: makeUser('Just text') })
    await flush()
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
    await wrapper.setProps({ message: makeUser('See @file:/workspace/new.png') })
    await flush()
    expect(wrapper.get('[data-testid="mention-images"] .relative').classes()).toContain('w-20')
    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
  })

  it('leaves assistant messages untouched even when they contain mention-like text', async () => {
    const wrapper = mountView(makeAssistant('Fix @file:/workspace/src/a.ts with @agent:plan'))
    await flush()

    expect(wrapper.findAll('[data-testid="mention-badge-file"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="mention-badge-agent"]')).toHaveLength(0)
    expect(wrapper.find('[data-testid="mention-images"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('@file:/workspace/src/a.ts')
  })
})
