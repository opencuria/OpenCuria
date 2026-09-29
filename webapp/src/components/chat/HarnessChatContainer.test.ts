import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import HarnessChatContainer from './HarnessChatContainer.vue'
import type { HarnessMessage } from '@/types/harness'

function makeMessage(overrides: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'msg-1',
    session_id: 'session-1',
    role: 'user',
    content: 'hello',
    parts: [],
    ...overrides,
  }
}

const HarnessMessageViewStub = {
  name: 'HarnessMessageView',
  props: ['message', 'streaming', 'childSessionIds'],
  template:
    '<div :data-message-id="message.id" :data-streaming="streaming ? \'1\' : \'0\'">{{ message.content }}</div>',
}

describe('HarnessChatContainer', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })
  it('keeps a local message without created_at at the end', () => {
    const wrapper = mount(HarnessChatContainer, {
      props: {
        messages: [
          makeMessage({
            id: 'older-user',
            content: 'older',
            created_at: '2026-03-29T10:00:00.000Z',
          }),
          makeMessage({
            id: 'older-assistant',
            role: 'assistant',
            content: 'previous reply',
            created_at: '2026-03-29T10:00:01.000Z',
          }),
          makeMessage({
            id: 'local-user',
            content: 'follow up',
          }),
        ],
      },
      global: {
        stubs: {
          HarnessMessageView: HarnessMessageViewStub,
        },
      },
    })

    const ids = wrapper
      .findAll('[data-message-id]')
      .map((node) => node.attributes('data-message-id'))
    expect(ids).toEqual(['older-user', 'older-assistant', 'local-user'])
  })

  it('sorts by backend position before creation time', () => {
    const wrapper = mount(HarnessChatContainer, {
      props: {
        messages: [
          makeMessage({
            id: 'b',
            content: 'b',
            position: 1,
            created_at: '2026-03-29T10:00:00.000Z',
          }),
          makeMessage({
            id: 'a',
            content: 'a',
            position: 0,
            created_at: '2026-03-29T10:00:05.000Z',
          }),
        ],
      },
      global: {
        stubs: {
          HarnessMessageView: HarnessMessageViewStub,
        },
      },
    })

    const ids = wrapper
      .findAll('[data-message-id]')
      .map((node) => node.attributes('data-message-id'))
    expect(ids).toEqual(['a', 'b'])
  })

  it('bounds transcript DOM for long histories while retaining all messages in memory', () => {
    const messages = Array.from({ length: 100 }, (_, index) =>
      makeMessage({ id: `message-${index}`, position: index }),
    )
    const wrapper = mount(HarnessChatContainer, {
      props: { messages },
      global: { stubs: { HarnessMessageView: HarnessMessageViewStub } },
    })
    expect(wrapper.findAll('[data-message-id]').length).toBeLessThan(40)
    expect(messages).toHaveLength(100)
  })

  it('mounts a fresh transcript when the history session changes', async () => {
    const wrapper = mount(HarnessChatContainer, {
      props: {
        messages: [makeMessage({ id: 'old-session-message', session_id: 'session-old' })],
      },
      global: { stubs: { HarnessMessageView: HarnessMessageViewStub } },
    })
    await wrapper.setProps({
      messages: [makeMessage({ id: 'new-session-message', session_id: 'session-new' })],
    })
    expect(wrapper.find('[data-message-id="old-session-message"]').exists()).toBe(false)
    expect(wrapper.find('[data-message-id="new-session-message"]').exists()).toBe(true)
  })

  it('uses measured heights and row gaps for virtual spacers and follows scroll input', async () => {
    const scheduledFrames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      scheduledFrames.push(callback)
      return scheduledFrames.length
    })
    vi.stubGlobal('cancelAnimationFrame', () => {})
    const resizeCallback: { current?: ResizeObserverCallback } = {}
    const measuredHeights = new Map<string, number>()
    let observed: Element[] = []
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback.current = callback
      }
      observe(target: Element): void {
        observed.push(target)
      }
      disconnect(): void {
        observed = []
      }
      unobserve(): void {}
    }
    vi.stubGlobal('ResizeObserver', FakeResizeObserver)
    const messages = Array.from({ length: 80 }, (_, index) =>
      makeMessage({ id: `geometry-${index}`, position: index }),
    )
    const wrapper = mount(HarnessChatContainer, {
      props: { messages },
      global: { stubs: { HarnessMessageView: HarnessMessageViewStub } },
    })
    await nextTick()
    const scroller = wrapper.element as HTMLElement
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 80 * 120 + 79 * 24 },
    })
    // Initial render is near the transcript end; report variable measured
    // rows through a fake ResizeObserver rather than relying on jsdom layout.
    const measuredNodes = observed.filter(
      (node) => node instanceof HTMLElement && node.dataset.messageId,
    )
    resizeCallback.current?.(
      measuredNodes.map((target) => {
        const id = (target as HTMLElement).dataset.messageId ?? ''
        const height = Number(id.endsWith('9') ? 60 : 80)
        measuredHeights.set(id, height)
        return { target, contentRect: { height } } as ResizeObserverEntry
      }),
      {} as ResizeObserver,
    )
    await nextTick()
    scroller.scrollTop = 5000
    let anchorTop = 100
    Object.defineProperty(scroller, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ top: 0, bottom: 400, left: 0, right: 0, width: 0, height: 400 }),
    })
    const positionNode = (node: Element): void => {
      const id = (node as HTMLElement).dataset.messageId ?? ''
      const index = Number(id.split('-').slice(-1)[0])
      Object.defineProperty(node, 'getBoundingClientRect', {
        configurable: true,
        value: () => {
          const top = index === 34 ? -100 : anchorTop + (index - 35) * 144
          return { top, bottom: top + 80, left: 0, right: 0, width: 0, height: 80 }
        },
      })
    }
    wrapper.findAll('[data-message-id]').forEach((node) => positionNode(node.element))
    scroller.dispatchEvent(new Event('scroll'))
    scheduledFrames.splice(0).forEach((frame) => frame(0))
    await nextTick()
    const renderedIds = wrapper
      .findAll('[data-message-id]')
      .map((node) => node.attributes('data-message-id'))
    expect(renderedIds).toContain('geometry-34')
    expect(renderedIds).not.toContain('geometry-0')
    const spacers = wrapper.findAll('[data-spacer-id]')
    const trailingSpacer = spacers[spacers.length - 1]
    expect(trailingSpacer?.exists()).toBe(true)
    // Each hidden row contributes its measured/estimated height and only
    // the gaps between hidden rows (outer flex gaps cover the boundaries).
    const spacerId = trailingSpacer?.attributes('data-spacer-id') ?? ''
    const hiddenStart = Number(spacerId.split('-')[1])
    const hiddenCount = messages.length - hiddenStart
    const hiddenHeight = messages.slice(hiddenStart).reduce((sum, message) => {
      return sum + (measuredHeights.get(message.id) ?? 120)
    }, 0)
    const expectedHeight = hiddenHeight + (hiddenCount - 1) * 24
    expect(trailingSpacer?.attributes('style')).toContain(`height: ${expectedHeight}px`)
    expect(wrapper.findAll('[data-message-id]').length).toBeLessThan(40)
    expect(scroller.scrollTop).toBe(5000)

    // A changed row above the saved first-visible anchor moves its DOM top;
    // fake that geometry shift and ensure the viewport compensates in scrollTop.
    // The virtual range changed after the first scroll; attach fake geometry
    // to the currently mounted nodes rather than relying on jsdom layout.
    wrapper.findAll('[data-message-id]').forEach((node) => positionNode(node.element))
    scroller.dispatchEvent(new Event('scroll'))
    // The visible anchor for this synthetic layout is geometry-35. Change its
    // visible box after scroll so the fake browser models content above it
    // expanding; ResizeObserver reports the resized earlier row.
    anchorTop = 200
    const anchor = wrapper.find('[data-message-id="geometry-35"]').element
    Object.defineProperty(anchor, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ top: 200, bottom: 280, left: 0, right: 0, width: 0, height: 80 }),
    })
    const preceding = wrapper.find('[data-message-id="geometry-29"]').element
    resizeCallback.current?.(
      [{ target: preceding, contentRect: { height: 180 } } as ResizeObserverEntry],
      {} as ResizeObserver,
    )
    await nextTick()
    expect(scroller.scrollTop).toBe(5100)
  })

  it('shows the skeleton only for the initial load, not over existing chat', () => {
    const skeletonOnly = mount(HarnessChatContainer, {
      props: { messages: [], loading: true },
      global: { stubs: { HarnessMessageView: HarnessMessageViewStub } },
    })
    expect(skeletonOnly.html()).toContain('animate-pulse')

    const withChat = mount(HarnessChatContainer, {
      props: { messages: [makeMessage({ id: 'user-1' })], loading: true },
      global: { stubs: { HarnessMessageView: HarnessMessageViewStub } },
    })
    expect(withChat.html()).not.toContain('animate-pulse')
    expect(withChat.find('[data-message-id="user-1"]').exists()).toBe(true)
  })

  it('marks only the live turn streaming via message id (fresh empty assistant)', () => {
    const wrapper = mount(HarnessChatContainer, {
      props: {
        streamingMessageId: 'assistant-fresh',
        messages: [
          makeMessage({ id: 'user-1', content: 'first', created_at: '2026-03-29T10:00:00.000Z' }),
          makeMessage({
            id: 'assistant-old',
            role: 'assistant',
            content: 'previous reply',
            created_at: '2026-03-29T10:00:01.000Z',
          }),
          makeMessage({ id: 'user-2', content: 'second', created_at: '2026-03-29T10:00:02.000Z' }),
          makeMessage({
            id: 'assistant-fresh',
            role: 'assistant',
            content: '',
            created_at: '2026-03-29T10:00:03.000Z',
          }),
        ],
      },
      global: {
        stubs: {
          HarnessMessageView: HarnessMessageViewStub,
        },
      },
    })

    const flags = wrapper.findAll('[data-message-id]').map((node) => ({
      id: node.attributes('data-message-id'),
      streaming: node.attributes('data-streaming'),
    }))
    // The previous answer never shows the cursor while the fresh empty
    // turn streams (Thinking renders inside the fresh turn instead).
    expect(flags).toEqual([
      { id: 'user-1', streaming: '0' },
      { id: 'assistant-old', streaming: '0' },
      { id: 'user-2', streaming: '0' },
      { id: 'assistant-fresh', streaming: '1' },
    ])
  })

  it('never marks a completed turn streaming, even with a stale anchor', () => {
    const wrapper = mount(HarnessChatContainer, {
      props: {
        streamingMessageId: 'assistant-old',
        messages: [
          makeMessage({
            id: 'assistant-old',
            role: 'assistant',
            content: 'done',
            completed_at: '2026-03-29T10:00:01.000Z',
          }),
          makeMessage({
            id: 'assistant-new',
            role: 'assistant',
            content: '',
          }),
        ],
      },
      global: {
        stubs: {
          HarnessMessageView: HarnessMessageViewStub,
        },
      },
    })

    const flags = wrapper.findAll('[data-message-id]').map((node) => ({
      id: node.attributes('data-message-id'),
      streaming: node.attributes('data-streaming'),
    }))
    expect(flags.every((flag) => flag.streaming === '0')).toBe(true)
  })

  it('marks only the last assistant message as streaming', () => {
    const wrapper = mount(HarnessChatContainer, {
      props: {
        streamingSessionId: 'session-1',
        messages: [
          makeMessage({
            id: 'user-1',
            content: 'first',
            created_at: '2026-03-29T10:00:00.000Z',
          }),
          makeMessage({
            id: 'assistant-1',
            role: 'assistant',
            content: 'previous reply',
            created_at: '2026-03-29T10:00:01.000Z',
          }),
          makeMessage({
            id: 'user-2',
            content: 'second',
            created_at: '2026-03-29T10:00:02.000Z',
          }),
          makeMessage({
            id: 'assistant-2',
            role: 'assistant',
            content: 'live',
            created_at: '2026-03-29T10:00:03.000Z',
          }),
        ],
      },
      global: {
        stubs: {
          HarnessMessageView: HarnessMessageViewStub,
        },
      },
    })

    const flags = wrapper.findAll('[data-message-id]').map((node) => ({
      id: node.attributes('data-message-id'),
      streaming: node.attributes('data-streaming'),
    }))
    expect(flags).toEqual([
      { id: 'user-1', streaming: '0' },
      { id: 'assistant-1', streaming: '0' },
      { id: 'user-2', streaming: '0' },
      { id: 'assistant-2', streaming: '1' },
    ])
  })
})
