import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

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
