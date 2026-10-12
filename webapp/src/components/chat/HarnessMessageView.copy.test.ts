import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import HarnessMessageView from './HarnessMessageView.vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return { ...actual, listProviderModels: vi.fn().mockResolvedValue([]) }
})

vi.mock('@/components/common/CopyButton.vue', () => ({
  default: {
    props: ['text', 'label', 'disabled'],
    template:
      "<button :data-testid=\"label === 'answer' ? 'answer-copy' : 'message-copy'\" :disabled=\"disabled\">{{ text }}</button>",
  },
}))

vi.mock('@/components/common/LoadingSpinner.vue', () => ({
  default: { template: '<span class="loading-stub" />' },
}))

vi.mock('./HarnessMarkdown.vue', () => ({
  default: {
    props: ['text', 'compact', 'onPrimary', 'streaming'],
    template: '<div class="markdown-stub">{{ text }}</div>',
  },
}))

function part(id: string, type: HarnessPart['type'], output = ''): HarnessPart {
  return { id, session_id: 'session-1', type, state: 'completed', title: '', output }
}

function message(
  role: HarnessMessage['role'],
  parts: HarnessPart[],
  content = '',
  id = 'message-1',
): HarnessMessage {
  return { id, session_id: 'session-1', role, content, parts }
}

function mountView(msg: HarnessMessage, streaming = false, disabled = false) {
  const tooltipStub = { template: '<div><slot /></div>' }
  return mount(HarnessMessageView, {
    props: { message: msg, streaming, disabled },
    global: {
      stubs: {
        Tooltip: tooltipStub,
        TooltipContent: tooltipStub,
        TooltipProvider: tooltipStub,
        TooltipTrigger: tooltipStub,
      },
    },
  })
}

describe('HarnessMessageView copy controls', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetProviderCatalogCache()
  })

  it('keeps user message copy available when edit/fork are disabled and during optimistic state', () => {
    const wrapper = mountView(message('user', [], 'prompt **markdown**'), false, true)
    expect(wrapper.get('[data-testid="message-copy"]').text()).toBe('prompt **markdown**')
    expect(wrapper.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('keeps user copy independent of edit/fork busy state and optimistic ids', async () => {
    const persisted = mountView(message('user', [], 'prompt', 'persisted-user'), false, true)
    expect(persisted.find('[data-testid="message-copy"]').exists()).toBe(true)
    expect(persisted.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(persisted.find('[data-testid="message-fork"]').exists()).toBe(false)

    const editing = mountView(message('user', [], 'prompt to edit', 'persisted-editing'))
    await editing.get('[data-testid="message-edit"]').trigger('click')
    expect(editing.find('[data-testid="message-copy"]').exists()).toBe(false)

    const local = mountView(message('user', [], 'local prompt', 'local-user-session-1-123'))
    expect(local.find('[data-testid="message-copy"]').exists()).toBe(true)
    expect(local.find('[data-testid="message-edit"]').exists()).toBe(false)
    expect(local.find('[data-testid="message-fork"]').exists()).toBe(false)
  })

  it('shows assistant copy only for the completed final answer text', () => {
    const wrapper = mountView(
      message('assistant', [
        part('t1', 'text', 'Earlier'),
        part('agent', 'agent', 'private plan'),
        part('tool', 'tool', 'private tool output'),
        part('final', 'text', 'Final answer'),
      ]),
    )
    expect(wrapper.get('[data-testid="answer-copy"]').text()).toBe('Final answer')
    expect(wrapper.get('[data-testid="harness-agent-step-action"]').text()).toBe('private plan')
    expect(wrapper.find('[data-testid="harness-worked-for"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="answer-copy"]').text()).not.toContain('private tool output')
    expect(wrapper.get('[data-testid="answer-copy"]').text()).not.toContain('private plan')
  })

  it('hides assistant copy while streaming, with no final prose, or when final prose is empty', () => {
    const streaming = mountView(message('assistant', [part('t1', 'text', 'Live')]), true)
    expect(streaming.find('[data-testid="message-copy"]').exists()).toBe(false)

    const noAnswer = mountView(message('assistant', [part('tool', 'tool', 'result')]))
    expect(noAnswer.find('[data-testid="message-copy"]').exists()).toBe(false)

    const emptyAnswer = mountView(
      message('assistant', [
        part('text', 'text', 'earlier'),
        part('tool', 'tool'),
        part('empty', 'text'),
      ]),
    )
    expect(emptyAnswer.find('[data-testid="message-copy"]').exists()).toBe(false)
  })
})
