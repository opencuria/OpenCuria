import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessContextSheet from './HarnessContextSheet.vue'

describe('HarnessContextSheet', () => {
  it('shows percent, token summary, and progress fill', () => {
    const wrapper = mount(HarnessContextSheet, {
      props: {
        context: {
          used: 64_400,
          limit: 200_000,
          percent: 32,
          promptTokens: 50_000,
          completionTokens: 14_400,
        },
      },
    })

    expect(wrapper.find('[data-testid="composer-context-percent"]').text()).toBe('32% Full')
    expect(wrapper.find('[data-testid="composer-context-tokens"]').text()).toBe(
      '~64.4K / 200.0K Tokens',
    )
    const bar = wrapper.find('[data-testid="composer-context-bar"] > div')
    expect(bar.attributes('style')).toContain('width: 32%')
    expect(wrapper.find('[data-testid="composer-context-breakdown"]').text()).toContain('50.0K')
    expect(wrapper.find('[data-testid="composer-context-breakdown"]').text()).toContain('14.4K')
  })

  it('shows unknown limit copy when the catalog limit is missing', () => {
    const wrapper = mount(HarnessContextSheet, {
      props: {
        context: { used: 1200, limit: 0, percent: 0 },
      },
    })

    expect(wrapper.find('[data-testid="composer-context-percent"]').text()).toBe('Unknown limit')
    expect(wrapper.find('[data-testid="composer-context-tokens"]').text()).toBe('~1.2K Tokens')
    const bar = wrapper.find('[data-testid="composer-context-bar"] > div')
    expect(bar.attributes('style')).toContain('width: 0%')
  })

  it('emits close when the header button is clicked', async () => {
    const wrapper = mount(HarnessContextSheet, {
      props: {
        context: { used: 100, limit: 1000, percent: 10 },
      },
    })

    await wrapper.find('[data-testid="composer-context-close"]').trigger('click')
    expect(wrapper.emitted('close')).toEqual([[]])
  })
})
