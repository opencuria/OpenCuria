import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessWorkedFor from './HarnessWorkedFor.vue'

describe('HarnessWorkedFor', () => {
  it('labels the header with the elapsed duration', () => {
    const wrapper = mount(HarnessWorkedFor, {
      props: { elapsedLabel: '5m 11s' },
      slots: { default: '<div data-testid="inner">row</div>' },
    })
    expect(wrapper.get('[data-testid="harness-worked-for-label"]').text()).toBe(
      'Worked for 5m 11s',
    )
    expect(wrapper.find('[data-testid="inner"]').exists()).toBe(false)
  })

  it('reveals the inner work when expanded', async () => {
    const wrapper = mount(HarnessWorkedFor, {
      props: { elapsedLabel: '11s' },
      slots: { default: '<div data-testid="inner">row</div>' },
    })
    await wrapper.get('[data-slot="collapsible-trigger"]').trigger('click')
    expect(wrapper.get('[data-testid="inner"]').text()).toBe('row')
  })
})
