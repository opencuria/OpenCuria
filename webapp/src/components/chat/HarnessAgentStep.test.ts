import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import HarnessAgentStep from './HarnessAgentStep.vue'
import type { HarnessPart } from '@/types/harness'

vi.mock('@/components/common/LoadingSpinner.vue', () => ({
  default: { template: '<span class="loading-stub" />' },
}))

vi.mock('./HarnessMarkdown.vue', () => ({
  default: {
    props: ['text', 'compact'],
    template: '<div class="markdown-stub">{{ text }}</div>',
  },
}))

function makeAgentPart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'agent-1',
    session_id: 'session-1',
    type: 'agent',
    state: 'completed',
    title: 'Agent plan',
    output:
      '(Previous action verification)\nprevious ok\n(Screenshot Analysis)\nlogin form\n(Next Action)\nclick submit\n(Grounded Action)\n```python\nagent.click("Submit")\n```',
    meta: {
      step: 2,
      agent_meta: {
        verification: 'previous ok',
        analysis: 'login form visible',
        next_action: 'click submit',
        action: 'click "Submit"',
        action_kind: 'click',
      },
    },
    ...overrides,
  }
}

describe('HarnessAgentStep', () => {
  it('renders a compact header with step eyebrow, action, and status', () => {
    const wrapper = mount(HarnessAgentStep, {
      props: { step: 2, part: makeAgentPart(), status: 'in_progress', live: true },
    })

    expect(wrapper.find('[data-testid="harness-agent-step"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Step 2')
    expect(wrapper.get('[data-testid="harness-agent-step-action"]').text()).toBe('Click "Submit"')
    const status = wrapper.get('[data-testid="harness-agent-step-status"]')
    expect(status.text()).toBe('In progress')
    expect(status.attributes('aria-live')).toBe('polite')
    expect(wrapper.find('.loading-stub').exists()).toBe(true)
  })

  it('keeps details collapsed until the trigger is activated', async () => {
    const wrapper = mount(HarnessAgentStep, {
      props: { step: 2, part: makeAgentPart(), status: 'completed' },
    })

    expect(wrapper.text()).not.toContain('login form visible')
    expect(wrapper.find('.markdown-stub').exists()).toBe(false)

    const trigger = wrapper.get('[data-testid="harness-agent-step-details-trigger"]')
    expect(trigger.attributes('aria-label')).toContain('Step 2')
    await trigger.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('login form visible')
    expect(wrapper.text()).toContain('previous ok')
    expect(wrapper.find('[data-testid="harness-agent-step-raw-trigger"]').exists()).toBe(true)
    expect(wrapper.find('.markdown-stub').exists()).toBe(false)

    await wrapper.get('[data-testid="harness-agent-step-raw-trigger"]').trigger('click')
    expect(wrapper.find('.markdown-stub').exists()).toBe(true)
  })

  it('renders legacy payloads with a raw-plan fallback instead of invented facts', async () => {
    const legacy = makeAgentPart({
      output: 'free-form older plan line one\nline two',
      meta: { step: 1 },
    })
    const wrapper = mount(HarnessAgentStep, {
      props: { step: 1, part: legacy, status: 'completed', legacy: true },
    })

    expect(wrapper.get('[data-testid="harness-agent-step-action"]').text()).toBe(
      'free-form older plan line one',
    )
    await wrapper.get('[data-testid="harness-agent-step-details-trigger"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Full plan text')
    expect(wrapper.text()).not.toContain('Analysis')

    await wrapper.get('[data-testid="harness-agent-step-raw-trigger"]').trigger('click')
    expect(wrapper.find('.markdown-stub').text()).toContain('free-form older plan')
  })

  it('renders fail actions as Report failure, done as Finish task', () => {
    const fail = mount(HarnessAgentStep, {
      props: {
        step: 4,
        part: makeAgentPart({
          id: 'agent-fail',
          meta: { step: 4, agent_meta: { action: 'fail', action_kind: 'fail' } },
        }),
        status: 'error',
      },
    })
    expect(fail.get('[data-testid="harness-agent-step-action"]').text()).toBe('Report failure')

    const done = mount(HarnessAgentStep, {
      props: {
        step: 5,
        part: makeAgentPart({
          id: 'agent-done',
          meta: { step: 5, agent_meta: { action: 'done', action_kind: 'done' } },
        }),
        status: 'completed',
      },
    })
    expect(done.get('[data-testid="harness-agent-step-action"]').text()).toBe('Finish task')
  })

  it('announces failed steps with icon and text (not color alone)', () => {    const wrapper = mount(HarnessAgentStep, {
      props: { step: 3, part: makeAgentPart({ id: 'agent-3' }), status: 'error' },
    })

    expect(wrapper.get('[data-testid="harness-agent-step"]').attributes('data-status')).toBe(
      'error',
    )
    expect(wrapper.get('[data-testid="harness-agent-step-status"]').text()).toBe('Failed')
    expect(wrapper.find('svg.lucide-circle-alert').exists()).toBe(true)
  })

  it('renders the full plan through a nested shadcn collapsible', async () => {
    const wrapper = mount(HarnessAgentStep, {
      props: { step: 2, part: makeAgentPart(), status: 'completed' },
    })

    await wrapper.get('[data-testid="harness-agent-step-details-trigger"]').trigger('click')
    await flushPromises()

    const rawTrigger = wrapper.get('[data-testid="harness-agent-step-raw-trigger"]')
    // shadcn CollapsibleTrigger keeps keyboard/ARIA semantics (button role,
    // aria-expanded, aria-controls) without an ad-hoc native button.
    expect(rawTrigger.attributes('aria-label')).toBe('Show full plan')
    expect(rawTrigger.attributes('aria-controls')).toBe('agent-step-plan-agent-1')
    expect(rawTrigger.element.tagName.toLowerCase()).toBe('button')
    expect(wrapper.find('button[data-testid="harness-agent-step-raw-trigger"]').exists()).toBe(true)

    await rawTrigger.trigger('click')
    await flushPromises()
    expect(rawTrigger.attributes('aria-label')).toBe('Hide full plan')
    expect(wrapper.find('.markdown-stub').exists()).toBe(true)
  })

  it('only connects the rail to a directly adjacent agent step', () => {
    const connected = mount(HarnessAgentStep, {
      props: { step: 1, part: makeAgentPart(), status: 'completed', connected: true },
    })
    const rail = connected.find('[data-testid="harness-agent-step-rail"]')
    expect(rail.exists()).toBe(true)
    expect(rail.attributes('data-connected')).toBe('true')

    const gap = mount(HarnessAgentStep, {
      props: { step: 1, part: makeAgentPart(), status: 'completed', connected: false },
    })
    expect(gap.find('[data-testid="harness-agent-step-rail"]').exists()).toBe(false)
  })
})
