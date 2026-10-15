import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import HarnessMessageFooter from './HarnessMessageFooter.vue'

function mountFooter(props: Partial<InstanceType<typeof HarnessMessageFooter>['$props']> = {}) {
  return mount(HarnessMessageFooter, {
    props: { copyLabel: 'answer', copyText: 'Answer', ...props },
  })
}

describe('HarnessMessageFooter', () => {
  it('uses one ordered action row with accessible labels and inline metadata', async () => {
    const wrapper = mountFooter({ forkable: true, metadata: 'Model · High · 1,204 in' })
    const buttons = wrapper.findAll('button')
    expect(buttons.map((button) => button.attributes('aria-label'))).toEqual([
      'Copy answer',
      'Fork chat after this answer',
      'Model · High · 1,204 in',
    ])
    await wrapper.get('[data-testid="message-fork"]').trigger('click')
    expect(wrapper.emitted('fork')).toEqual([[]])
    expect(wrapper.get('[data-testid="harness-message-usage"] span').classes()).toContain(
      'truncate',
    )
  })

  it.each([
    { copyLabel: 'message' as const, editable: true },
    { copyLabel: 'answer' as const, forkable: true },
    { streaming: true, metadata: 'Model High' },
  ])('uses the same responsive visibility for every footer: %o', (props) => {
    const footer = mountFooter(props).get('[data-testid="message-footer"]')
    expect(footer.classes()).toContain('opacity-100')
    expect(footer.classes()).toContain('md:opacity-0')
    expect(footer.classes()).toContain('md:group-hover/message:opacity-100')
    expect(footer.classes()).toContain('md:group-focus-within/message:opacity-100')
    expect(footer.classes()).toContain('md:focus-within:opacity-100')
    expect(footer.classes()).toContain('[@media(hover:none)]:opacity-100')
  })

  it('uses compact, equally sized actions and smaller inline metadata', () => {
    const wrapper = mountFooter({ forkable: true, metadata: 'Model · High' })
    expect(wrapper.get('[data-testid="message-footer"]').classes()).toContain('mt-0.5')
    const actions = ['answer-copy', 'message-fork'].map((id) =>
      wrapper.get(`[data-testid="${id}"]`),
    )
    for (const action of actions) {
      expect(action.classes()).toContain('h-7')
      expect(action.classes()).toContain('w-7')
      expect(action.classes()).toContain('[&_svg:not([class*=size-])]:size-3')
      expect(action.classes()).toContain('[@media(pointer:coarse)]:h-8')
      expect(action.classes()).toContain('[@media(pointer:coarse)]:w-8')
    }
    expect(wrapper.get('[data-testid="harness-message-usage"]').classes()).toContain('h-7')
    expect(wrapper.get('[data-testid="harness-message-usage"]').classes()).toContain('text-[11px]')
  })

  it('opens complete metadata on click for touch users', async () => {
    const wrapper = mountFooter({ metadata: 'Model · High · 1,204 in' })
    await wrapper.get('[data-testid="harness-message-usage"]').trigger('click')
    const popover = wrapper.findComponent({ name: 'Popover' })
    expect(popover.emitted('update:open')).toEqual([[true]])
    wrapper.unmount()
  })

  it('right-aligns user actions and emits edit independently of fork', async () => {
    const wrapper = mountFooter({ copyLabel: 'message', align: 'end', editable: true })
    expect(wrapper.get('[data-testid="message-footer"]').classes()).toContain('justify-end')
    expect(wrapper.find('[data-testid="message-fork"]').exists()).toBe(false)
    await wrapper.get('[data-testid="message-edit"]').trigger('click')
    expect(wrapper.emitted('edit')).toEqual([[]])
  })
})
